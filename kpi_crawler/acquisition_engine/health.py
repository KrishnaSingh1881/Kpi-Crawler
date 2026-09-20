"""Shared failure-counting state machine used by SessionManager and ProxyPool.

This module contains only the counting/threshold logic — it knows nothing
about Session health enums, proxy quarantine states, or any network
identity concept.  Each caller holds one FailureCounter per tracked item
and delegates counting to it while keeping its own state transitions and
locking.
"""


class FailureCounter:
    """Tracks consecutive failures and signals when the unhealthy threshold
    is crossed.

    Thread-safety is the *caller's* responsibility (SessionManager and
    ProxyPool both hold their own locks and call into this object while
    holding them).

    Args:
        unhealthy_after_failures: Number of consecutive failures that must
            accumulate before :meth:`report_failure` returns ``True``.
    """

    def __init__(self, *, unhealthy_after_failures: int) -> None:
        self._threshold = unhealthy_after_failures
        self.consecutive_failures: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def report_success(self) -> bool:
        """Reset the consecutive-failure counter.

        Returns:
            ``True`` if there were any consecutive failures before this
            success (i.e. the item was in a degraded state and has now
            recovered), ``False`` if it was already clean.  Callers can
            use this to emit a "recovered" event exactly once.
        """
        had_failures = self.consecutive_failures > 0
        self.consecutive_failures = 0
        return had_failures

    def report_failure(self) -> bool:
        """Increment the consecutive-failure counter.

        Returns:
            ``True`` exactly when the counter reaches (or is already at or
            beyond) the threshold — i.e. the item should now be considered
            unhealthy/quarantined.  ``False`` while still below the
            threshold.
        """
        self.consecutive_failures += 1
        return self.consecutive_failures >= self._threshold
