"""The stable Program 1 -> Program 2 acquisition contract.

Everything in this module is what a future consumer depends on. Internal
engine changes (session handling, proxy pool, adaptive policy, HTTP vs
browser acquisition) must never require a change here. Bump
`CONTRACT_VERSION` for any breaking change to these shapes.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

CONTRACT_VERSION = 2

# v2: added AcquisitionState.NOT_FOUND. A 404 (resource genuinely does not
# exist) was previously folded into NETWORK_ERROR, which made it
# indistinguishable from a real connectivity failure to both a human reading
# `final_result` and to the session/proxy health signals that key off it —
# a session or proxy was penalized identically for "the site is blocking us"
# and "this URL was never there in the first place". NOT_FOUND is a
# TERMINAL_FAILURE_STATE like the others; nothing about ARTIFACT_PRODUCING
# or the run-status rollup changes.


class AcquisitionState(str, Enum):
    """The full, explicit set of per-attempt/per-run outcomes.

    Deliberately not a bare bool or a two-state success/failure model: a run
    is PARTIAL, not FAILED, when some but not all resources were acquired,
    and a failure always carries a specific classification.
    """

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    ACCESS_DENIED = "ACCESS_DENIED"
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    NOT_FOUND = "NOT_FOUND"
    BROWSER_ERROR = "BROWSER_ERROR"
    UNSUPPORTED = "UNSUPPORTED"
    BLOCKED = "BLOCKED"
    PENDING_RETRY = "PENDING_RETRY"
    # Run-level only: the operator stopped the run (e.g. Ctrl+C) before it
    # finished. Distinct from FAILED (nothing succeeded) and PARTIAL (the run
    # ran to completion with mixed results) — an interrupted run may have
    # acquired plenty and simply been cut short. Never a per-attempt
    # `final_result`.
    INTERRUPTED = "INTERRUPTED"


# States that represent an artifact having actually been acquired.
ARTIFACT_PRODUCING_STATES = frozenset({AcquisitionState.SUCCESS})

# States that represent a resource never being acquired at all (as opposed to
# a run-level rollup like PARTIAL/FAILED, which never appears on an attempt).
TERMINAL_FAILURE_STATES = frozenset(
    {
        AcquisitionState.RATE_LIMITED,
        AcquisitionState.ACCESS_DENIED,
        AcquisitionState.TIMEOUT,
        AcquisitionState.NETWORK_ERROR,
        AcquisitionState.NOT_FOUND,
        AcquisitionState.BROWSER_ERROR,
        AcquisitionState.UNSUPPORTED,
        AcquisitionState.BLOCKED,
    }
)


@dataclass(frozen=True)
class RedirectHop:
    url: str
    status: int


@dataclass(frozen=True)
class ArtifactRecord:
    """One successfully acquired resource. The only thing Program 2 needs to
    read a resource's bytes and know where they came from — nothing about
    Crawlee/Playwright/sessions/proxies/retries leaks into this shape.
    """

    contract_version: int
    artifact_id: int
    run_id: int
    source_url: str
    canonical_url: str | None
    discovered_from: str | None
    fetched_at: datetime
    content_type: str | None
    http_status: int | None
    acquisition_method: str  # "http" | "browser"
    raw_location: str
    content_size: int
    checksum: str  # sha256 hex
    encoding: str | None
    final_url: str | None
    redirect_chain: tuple[RedirectHop, ...]
    session_id: str | None
    status: AcquisitionState


@dataclass(frozen=True)
class AttemptRecord:
    """One acquisition attempt — the Evidence Ledger's unit of record.

    Every attempt, successful or not, produces exactly one of these. Nothing
    is overwritten: a URL retried 4 times has 4 AttemptRecords.
    """

    run_id: int
    attempt_id: int
    occurred_at: datetime
    url: str
    domain: str
    acquisition_method: str  # "http" | "browser"
    session_id: str | None
    proxy_id: str | None
    proxy_status: str | None
    http_status: int | None
    latency_ms: float | None
    retry_number: int
    retry_budget: int | None
    timeout_seconds: float | None
    backoff_applied_seconds: float | None
    concurrency_at_attempt: int | None
    rate_limit_detected: bool
    failure_classification: str | None
    adaptive_decision: str | None
    final_result: AcquisitionState
    artifact_id: int | None
    error_message: str | None = None


@dataclass(frozen=True)
class AdaptiveDecisionRecord:
    """One decision made by the adaptive policy engine, independent of any
    single attempt — e.g. "429 detected -> concurrency 8 -> 4" applies to the
    whole domain's in-flight work, not one URL.
    """

    run_id: int
    decision_id: int
    occurred_at: datetime
    domain: str | None
    decision_type: str
    reason: str
    before_value: str | None
    after_value: str | None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunSummary:
    """`attempted`, `acquired`, `failed`, `retries`, `rate_limited`,
    `browser_pages`, `proxy_failures`, and `by_state` are all derived
    directly from Evidence Ledger rows (`EvidenceLedger.summarize_run`) —
    never a separately incremented counter that could drift from what the
    ledger actually recorded. `discovered` is the one exception: it counts
    every distinct URL the crawl's link discovery ever surfaced, including
    ones filtered out before any attempt was made (off-host, over budget,
    beyond max depth) — the ledger has no record of those at all, since
    nothing was ever attempted for them, so this number comes from the
    engine's own discovery bookkeeping rather than the ledger. `partial` is
    always 0 in the current model: no per-attempt `AcquisitionState` means
    "partially acquired" (an attempt either produces a complete artifact or
    it doesn't), so there is nothing to count yet — it is reserved for a
    future partial-content concept, not a decorative placeholder.
    """

    run_id: int
    contract_version: int
    root_source_url: str | None
    started_at: datetime
    completed_at: datetime | None
    status: AcquisitionState
    discovered: int
    attempted: int
    acquired: int
    partial: int
    failed: int
    retries: int
    rate_limited: int
    browser_pages: int
    proxy_failures: int
    by_state: dict[str, int]


def derive_run_status(final_results_by_url: dict[str, AcquisitionState]) -> AcquisitionState:
    """SUCCESS/PARTIAL/FAILED rollup from each URL's own final outcome.

    Never collapses a run with any successful artifact to FAILED, and never
    calls a run SUCCESS if anything failed — that is exactly what PARTIAL is
    for.
    """
    if not final_results_by_url:
        return AcquisitionState.FAILED
    successes = sum(1 for s in final_results_by_url.values() if s in ARTIFACT_PRODUCING_STATES)
    total = len(final_results_by_url)
    if successes == total:
        return AcquisitionState.SUCCESS
    if successes == 0:
        return AcquisitionState.FAILED
    return AcquisitionState.PARTIAL
