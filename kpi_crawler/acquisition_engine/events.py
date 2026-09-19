"""Structured events emitted by the acquisition engine.

The CLI (or any other presentation layer, or a test) is purely a consumer of
these — no business logic about what counts as "important" or how to
aggregate progress lives in a terminal-printing function. `EventSink` is the
one interface the engine talks to.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .contract import AcquisitionState, AttemptRecord, RunSummary


@dataclass(frozen=True)
class ProgressSnapshot:
    """Aggregated, periodic normal-activity update."""

    at: datetime
    elapsed_seconds: float
    current_domain: str | None
    discovered: int
    processed: int
    acquired: int
    partial: int
    failed: int
    retries: int
    concurrency: int
    request_rate_per_s: float
    active_browser_sessions: int


@dataclass(frozen=True)
class ImportantEvent:
    """A meaningful decision or state change, shown immediately regardless of
    aggregation cadence: run start, target, discovery progress, rate
    limiting, proxy health, method escalation, session lifecycle, retry
    exhaustion, concurrency recovery, run outcome.

    `data` is optional structured detail for consumers that want it (tests,
    a future non-terminal presentation) without parsing `detail_lines`
    strings; it is never required and never contains anything the terminal
    renderer doesn't also show in the text.
    """

    at: datetime
    icon: str
    headline: str
    detail_lines: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BackoffEvent:
    """Emitted for the duration of a backoff wait so the terminal never goes
    blank while the engine is deliberately idle.
    """

    at: datetime
    seconds: float
    reason: str
    domain: str
    concurrency: int


@dataclass(frozen=True)
class AttemptEvent:
    """Verbose-only: one raw attempt, as recorded in the evidence ledger.

    `fingerprint_summary` is presentation-only enrichment, never persisted:
    for a browser-method attempt, a short, safe descriptor of the session's
    injected fingerprint (platform + a short non-reversible tag) — never the
    full profile, and never anything from `ProxyConfig`/headers that could
    carry credentials.
    """

    attempt: AttemptRecord
    fingerprint_summary: str | None = None


@dataclass(frozen=True)
class FinalSummaryEvent:
    summary: RunSummary
    evidence_path: str
    adaptive_action_counts: dict[str, int] = field(default_factory=dict)


AcquisitionEvent = ProgressSnapshot | ImportantEvent | BackoffEvent | AttemptEvent | FinalSummaryEvent


class EventSink(Protocol):
    def emit(self, event: AcquisitionEvent) -> None: ...


@dataclass
class ListEventSink:
    """Collects every event in order — used by tests and by any consumer that
    wants the full event history rather than a live terminal render.
    """

    events: list[AcquisitionEvent] = field(default_factory=list)

    def emit(self, event: AcquisitionEvent) -> None:
        self.events.append(event)


class NullEventSink:
    def emit(self, event: AcquisitionEvent) -> None:
        pass
