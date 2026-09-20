"""Session lifecycle tracking, shared by the HTTP and browser acquirers.

A "session" here is a logical unit of acquisition identity/health (e.g. one
browser context, or one HTTP client identity) — this module tracks its
health and retirement, independent of what actually backs it (a Playwright
`BrowserContext`, a `urllib` connection, or nothing at all for a stateless
plain request).

Thread-safe (backed by a single lock): sessions are acquired, reported on,
and retired concurrently from the engine's worker threads.
"""

from dataclasses import dataclass, field
from enum import Enum
import itertools
import threading

from kpi_crawler.acquisition_engine.health import FailureCounter


class SessionHealth(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    RETIRED = "retired"


@dataclass
class Session:
    session_id: str
    health: SessionHealth = SessionHealth.HEALTHY
    requests_served: int = 0
    # unhealthy_after_failures is set by SessionManager when creating a session
    _failure_counter: FailureCounter = field(default_factory=lambda: FailureCounter(unhealthy_after_failures=3))

    @property
    def consecutive_failures(self) -> int:  # kept for any callers that read it directly
        return self._failure_counter.consecutive_failures


class SessionManager:
    """Creates, tracks health of, and retires sessions. Does not itself own
    any network/browser resource — callers (http_acquirer/browser_acquirer)
    attach their own resource to a session_id and clean it up when this
    manager reports the session retired.

    `acquire(domain)` is the identity-persistence entry point a crawl should
    use: it hands out a small, reused pool of sessions per domain (growing
    up to `pool_size`, then round-robining among the pool) so a domain sees
    a consistent handful of browsing identities across a run instead of a
    brand-new one per URL — which is what made the fingerprint-consistency
    work in `browser_acquirer.py` actually matter during a real crawl, not
    just in isolation. `retire` removes a session from its domain's pool so
    the next `acquire` for that domain mints a fresh replacement.

    `create()` remains available for a standalone session with no pool
    membership (tests, or a caller acquiring a single explicit URL with no
    notion of "domain crawl").
    """

    def __init__(self, *, unhealthy_after_failures: int = 3, prefix: str = "session", pool_size: int = 4):
        self._unhealthy_after_failures = unhealthy_after_failures
        self._prefix = prefix
        self._pool_size = pool_size
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._pools: dict[str, list[str]] = {}
        self._pool_cursor: dict[str, int] = {}

    def _create_locked(self) -> Session:
        session_id = f"{self._prefix}_{next(self._counter)}"
        session = Session(
            session_id=session_id,
            _failure_counter=FailureCounter(unhealthy_after_failures=self._unhealthy_after_failures),
        )
        self._sessions[session_id] = session
        return session

    def create(self) -> Session:
        with self._lock:
            return self._create_locked()

    def acquire(self, domain: str) -> Session:
        """Return a session to use for `domain`, reusing one already in that
        domain's pool where possible instead of minting a fresh identity for
        every call.
        """
        with self._lock:
            pool = self._pools.setdefault(domain, [])
            if len(pool) < self._pool_size:
                session = self._create_locked()
                pool.append(session.session_id)
                return session
            idx = self._pool_cursor.get(domain, 0) % len(pool)
            self._pool_cursor[domain] = (idx + 1) % len(pool)
            return self._sessions[pool[idx]]

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def report_success(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session._failure_counter.report_success()
            session.requests_served += 1
            if session.health == SessionHealth.UNHEALTHY:
                session.health = SessionHealth.HEALTHY

    def report_failure(self, session_id: str) -> SessionHealth:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return SessionHealth.RETIRED
            threshold_crossed = session._failure_counter.report_failure()
            if threshold_crossed:
                session.health = SessionHealth.UNHEALTHY
            return session.health

    def retire(self, session_id: str, domain: str | None = None) -> None:
        """Mark a session retired. When `domain` is given, also drop it from
        that domain's pool so the next `acquire(domain)` mints a fresh
        replacement rather than round-robining onto a retired session.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.health = SessionHealth.RETIRED
            if domain is not None:
                pool = self._pools.get(domain)
                if pool and session_id in pool:
                    pool.remove(session_id)
                    self._pool_cursor[domain] = 0

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._sessions.values() if s.health != SessionHealth.RETIRED)
