"""Session lifecycle tracking, shared by the HTTP and browser acquirers.

A "session" here is a logical unit of acquisition identity/health (e.g. one
browser context, or one HTTP client identity) — this module tracks its
health and retirement, independent of what actually backs it (a Playwright
`BrowserContext`, a `urllib` connection, or nothing at all for a stateless
plain request).
"""

from dataclasses import dataclass, field
from enum import Enum
import itertools


class SessionHealth(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    RETIRED = "retired"


@dataclass
class Session:
    session_id: str
    health: SessionHealth = SessionHealth.HEALTHY
    consecutive_failures: int = 0
    requests_served: int = 0


class SessionManager:
    """Creates, tracks health of, and retires sessions. Does not itself own
    any network/browser resource — callers (http_acquirer/browser_acquirer)
    attach their own resource to a session_id and clean it up when this
    manager reports the session retired.
    """

    def __init__(self, *, unhealthy_after_failures: int = 3, prefix: str = "session"):
        self._unhealthy_after_failures = unhealthy_after_failures
        self._prefix = prefix
        self._counter = itertools.count(1)
        self._sessions: dict[str, Session] = {}

    def create(self) -> Session:
        session_id = f"{self._prefix}_{next(self._counter)}"
        session = Session(session_id=session_id)
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def report_success(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.consecutive_failures = 0
        session.requests_served += 1
        if session.health == SessionHealth.UNHEALTHY:
            session.health = SessionHealth.HEALTHY

    def report_failure(self, session_id: str) -> SessionHealth:
        session = self._sessions.get(session_id)
        if session is None:
            return SessionHealth.RETIRED
        session.consecutive_failures += 1
        if session.consecutive_failures >= self._unhealthy_after_failures:
            session.health = SessionHealth.UNHEALTHY
        return session.health

    def retire(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            session.health = SessionHealth.RETIRED

    def active_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.health != SessionHealth.RETIRED)
