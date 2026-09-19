"""The adaptive policy engine: per-domain concurrency control, backoff
computation, and rate-limit detection.

Thread-safe (backed by a single lock) since it is shared across the acquirer's
worker threads. Pure decision logic — it never itself sleeps, retires a
session, or makes a request; callers act on the `PolicySignal`s it returns
and are responsible for recording/emitting them.
"""

from dataclasses import dataclass, field
import threading


@dataclass(frozen=True)
class PolicySignal:
    """A concurrency-affecting decision. `backoff_seconds` is populated only
    on rate-limit signals; concurrency-restore signals never carry a wait.
    """

    decision_type: str  # "concurrency_reduced" | "concurrency_restored"
    reason: str
    before_value: int
    after_value: int
    backoff_seconds: float | None = None


@dataclass
class _DomainState:
    concurrency: int
    consecutive_successes: int = 0
    consecutive_rate_limits: int = 0


class AdaptivePolicy:
    def __init__(
        self,
        *,
        initial_concurrency: int = 8,
        min_concurrency: int = 1,
        max_concurrency: int = 8,
        base_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 60.0,
        restore_after_successes: int = 5,
    ):
        self.initial_concurrency = initial_concurrency
        self.min_concurrency = min_concurrency
        self.max_concurrency = max_concurrency
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.restore_after_successes = restore_after_successes
        self._lock = threading.Lock()
        self._domains: dict[str, _DomainState] = {}

    def _state(self, domain: str) -> _DomainState:
        state = self._domains.get(domain)
        if state is None:
            state = _DomainState(concurrency=self.initial_concurrency)
            self._domains[domain] = state
        return state

    def current_concurrency(self, domain: str) -> int:
        with self._lock:
            return self._state(domain).concurrency

    def on_rate_limited(self, domain: str) -> PolicySignal:
        """A 429 (or equivalent) was observed for this domain. Always returns
        a backoff duration; only includes a concurrency change when the
        concurrency actually moved (so callers don't emit a no-op decision).
        """
        with self._lock:
            state = self._state(domain)
            state.consecutive_rate_limits += 1
            state.consecutive_successes = 0
            before = state.concurrency
            after = max(self.min_concurrency, state.concurrency // 2)
            state.concurrency = after
            backoff = min(
                self.max_backoff_seconds,
                self.base_backoff_seconds * (2 ** (state.consecutive_rate_limits - 1)),
            )
            return PolicySignal(
                decision_type="concurrency_reduced" if after != before else "backoff",
                reason="429 detected",
                before_value=before,
                after_value=after,
                backoff_seconds=backoff,
            )

    def on_success(self, domain: str) -> PolicySignal | None:
        with self._lock:
            state = self._state(domain)
            state.consecutive_rate_limits = 0
            state.consecutive_successes += 1
            if state.consecutive_successes >= self.restore_after_successes and state.concurrency < self.max_concurrency:
                before = state.concurrency
                state.concurrency = min(self.max_concurrency, state.concurrency + 1)
                state.consecutive_successes = 0
                return PolicySignal(
                    decision_type="concurrency_restored",
                    reason="healthy period detected",
                    before_value=before,
                    after_value=state.concurrency,
                )
            return None
