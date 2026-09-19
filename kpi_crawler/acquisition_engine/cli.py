"""Terminal presentation layer: consumes structured acquisition events and
renders them as live, human-readable progress. No business logic lives here
— every decision about what happened was already made by the engine/ledger;
this module only formats it.

Normal mode: run start/target, discovery progress, aggregated progress,
important events, backoff visibility, and a final summary (with a failure
breakdown when the run is not a clean SUCCESS).
--verbose: adds one block per raw acquisition attempt.

Deliberately line-based, not a curses/ANSI live-updating display: every
event is one `print(..., flush=True)` call. This works identically in an
interactive terminal, a redirected-to-file/CI log, and a pipe — there is no
"fallback" branch needed because there is no TTY-only code path to fall back
from.
"""

import argparse
from datetime import datetime
from pathlib import Path
import re
import signal
import sys

from ..config import ConfigurationError, Settings
from ..db import connection
from ..errors import ApplicationError
from ..logging import configure_logging
from .contract import AcquisitionState
from .engine import AcquisitionEngine, EngineConfig
from .events import AttemptEvent, BackoffEvent, EventSink, FinalSummaryEvent, ImportantEvent, ProgressSnapshot
from .ledger import EvidenceLedger

_SEP = "─" * 46

# Exit codes. 0/1 match every other command in this project (success / clean
# application error); the run-outcome codes are new and specific to this
# command, chosen to avoid colliding with argparse's own usage-error code (2).
EXIT_SUCCESS = 0
EXIT_CONFIG_ERROR = 1
EXIT_PARTIAL = 3
EXIT_FAILED = 4
EXIT_INTERRUPTED = 5

_EXIT_CODE_BY_STATUS = {
    AcquisitionState.SUCCESS: EXIT_SUCCESS,
    AcquisitionState.PARTIAL: EXIT_PARTIAL,
    AcquisitionState.FAILED: EXIT_FAILED,
    AcquisitionState.INTERRUPTED: EXIT_INTERRUPTED,
}

# Strips embedded userinfo (user:pass@) from any URL-shaped substring before
# it reaches the terminal — a proxy configured as http://user:secret@host is
# the one place this contract otherwise carries a URL with credentials. Every
# free-text field (headline, detail lines, attempt url/error message) is
# passed through this before printing.
_CREDENTIALS_IN_URL_RE = re.compile(r"(://)[^/@\s]+:[^/@\s]+@")


def _redact(text: str) -> str:
    return _CREDENTIALS_IN_URL_RE.sub(r"\1", text)


def _ts(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def _format_progress(event: ProgressSnapshot) -> str:
    line = f"[{_ts(event.at)}] ✓ {event.acquired}/{event.discovered} acquired"
    extras = [
        f"domain={event.current_domain}" if event.current_domain else None,
        f"processed={event.processed}",
        f"failed={event.failed}" if event.failed else None,
        f"retries={event.retries}" if event.retries else None,
        f"concurrency={event.concurrency}",
        f"rate={event.request_rate_per_s:.1f}/s",
        f"browser_sessions={event.active_browser_sessions}" if event.active_browser_sessions else None,
        f"elapsed={event.elapsed_seconds:.0f}s",
    ]
    return line + " | " + " | ".join(e for e in extras if e)


def _format_important(event: ImportantEvent) -> list[str]:
    lines = [f"[{_ts(event.at)}] {event.icon} {_redact(event.headline)}"]
    lines.extend(f"   {_redact(detail)}" for detail in event.detail_lines if detail)
    return lines


def _format_backoff(event: BackoffEvent) -> list[str]:
    return [
        f"[{_ts(event.at)}] ⏳ Backing off {event.seconds:.0f}s",
        f"   Reason: {event.reason}",
        f"   Domain: {_redact(event.domain)}",
        f"   Concurrency: {event.concurrency}",
    ]


def _format_attempt(event: AttemptEvent) -> list[str]:
    attempt = event.attempt
    lines = [
        f"[attempt] id=att_{attempt.attempt_id}",
        f"[url] {_redact(attempt.url)}",
        f"[timestamp] {attempt.occurred_at.isoformat()}",
        f"[method] {attempt.acquisition_method}",
        f"[session] {attempt.session_id}",
    ]
    if event.fingerprint_summary:
        lines.append(f"[fingerprint] {event.fingerprint_summary}")
    if attempt.proxy_id:
        lines.append(f"[proxy] {attempt.proxy_id} ({attempt.proxy_status})")
    if attempt.http_status is not None:
        lines.append(f"[status] {attempt.http_status}")
    if attempt.latency_ms is not None:
        lines.append(f"[latency] {attempt.latency_ms:.0f}ms")
    lines.append(f"[retry] {attempt.retry_number}/{attempt.retry_budget}")
    if attempt.backoff_applied_seconds:
        lines.append(f"[backoff] {attempt.backoff_applied_seconds:.0f}s")
    if attempt.adaptive_decision:
        lines.append(f"[policy] {attempt.adaptive_decision}")
    lines.append("[evidence] recorded")
    lines.append(f"[artifact] {attempt.artifact_id if attempt.artifact_id is not None else '(none)'}")
    if attempt.error_message:
        lines.append(f"[error] {_redact(attempt.error_message)}")
    lines.append(f"[result] {attempt.final_result.value}")
    return lines


_FAILURE_LABELS = {
    AcquisitionState.RATE_LIMITED: "RATE_LIMITED",
    AcquisitionState.ACCESS_DENIED: "ACCESS_DENIED",
    AcquisitionState.TIMEOUT: "TIMEOUT",
    AcquisitionState.NETWORK_ERROR: "NETWORK_ERROR",
    AcquisitionState.BROWSER_ERROR: "BROWSER_ERROR",
    AcquisitionState.UNSUPPORTED: "UNSUPPORTED",
    AcquisitionState.BLOCKED: "BLOCKED",
    AcquisitionState.PENDING_RETRY: "PENDING_RETRY",
}

_ADAPTIVE_ACTION_LABELS = {
    "concurrency_reduced": "concurrency reduced",
    "concurrency_restored": "concurrency restored",
    "proxy_quarantined": "proxy quarantined",
    "proxy_recovered": "proxy recovered",
    "method_escalated": "escalated to browser",
}


def _format_failure_summary(event: FinalSummaryEvent) -> list[str] | None:
    """The condensed breakdown shown under the result box for any run that
    isn't a clean SUCCESS. Every number here comes straight from
    `RunSummary.by_state`/`retries` (ledger-derived) and
    `adaptive_action_counts` (from `acq.adaptive_decisions`) — nothing here
    is computed independently of what the engine actually recorded.
    """
    s = event.summary
    if s.status == AcquisitionState.SUCCESS:
        return None

    lines = [f"Status: {s.status.value}", ""]

    failure_lines = [
        f"- {count} {_FAILURE_LABELS.get(AcquisitionState(state), state)}"
        for state, count in sorted(s.by_state.items(), key=lambda kv: -kv[1])
        if state != AcquisitionState.SUCCESS.value and count
    ]
    if failure_lines:
        lines.append("Failures:")
        lines.extend(failure_lines)
        lines.append("")

    action_lines = []
    for action, count in event.adaptive_action_counts.items():
        label = _ADAPTIVE_ACTION_LABELS.get(action, action)
        action_lines.append(f"- {label} ({count}x)")
    if s.retries:
        action_lines.append(f"- {s.retries} retries performed")
    if action_lines:
        lines.append("Adaptive actions:")
        lines.extend(action_lines)
        lines.append("")

    lines.append(f"Evidence: {event.evidence_path}")
    return lines


def _format_final_summary(event: FinalSummaryEvent) -> str:
    s = event.summary
    duration = (s.completed_at - s.started_at) if s.completed_at else None
    duration_str = f"{int(duration.total_seconds() // 60)}m {int(duration.total_seconds() % 60)}s" if duration else "n/a"
    lines = [
        _SEP,
        "ACQUISITION RESULT",
        _SEP,
        f"Run ID          : {s.run_id}",
        f"Target          : {_redact(s.root_source_url or '(unknown)')}",
        "",
        f"Discovered      : {s.discovered}",
        f"Attempted       : {s.attempted}",
        f"Successful      : {s.acquired}",
        f"Partial         : {s.partial}",
        f"Failed          : {s.failed}",
        f"Retries         : {s.retries}",
        f"Rate limits     : {s.rate_limited}",
        f"Browser pages   : {s.browser_pages}",
        f"Proxy failures  : {s.proxy_failures}",
        "",
        f"Duration        : {duration_str}",
        f"Evidence        : {event.evidence_path}",
        "",
        f"Status          : {s.status.value}",
        _SEP,
    ]
    failure_summary = _format_failure_summary(event)
    if failure_summary:
        lines.append("")
        lines.extend(failure_summary)
    return "\n".join(lines)


class TerminalEventSink(EventSink):
    def __init__(self, *, verbose: bool, stream=sys.stdout):
        self._verbose = verbose
        self._stream = stream

    def emit(self, event) -> None:
        if isinstance(event, ProgressSnapshot):
            print(_format_progress(event), file=self._stream, flush=True)
        elif isinstance(event, ImportantEvent):
            for line in _format_important(event):
                print(line, file=self._stream, flush=True)
        elif isinstance(event, BackoffEvent):
            for line in _format_backoff(event):
                print(line, file=self._stream, flush=True)
        elif isinstance(event, AttemptEvent):
            if self._verbose:
                for line in _format_attempt(event):
                    print(line, file=self._stream, flush=True)
                print(file=self._stream, flush=True)
        elif isinstance(event, FinalSummaryEvent):
            print(_format_final_summary(event), file=self._stream, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kpi-crawler-acquire")
    parser.add_argument("source_url")
    parser.add_argument("--verbose", action="store_true", help="show per-attempt request/session/proxy/fingerprint detail")
    parser.add_argument("--max-depth", type=int, default=2)
    parser.add_argument("--max-artifacts", type=int, default=50)
    parser.add_argument("--max-concurrency", type=int, default=8)
    parser.add_argument("--no-browser", action="store_true", help="disable escalation to browser acquisition")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        settings = Settings.from_environment()
        configure_logging(settings.log_level)
        args = build_parser().parse_args(argv)

        engine_config = EngineConfig(
            max_depth=args.max_depth,
            max_artifacts=args.max_artifacts,
            max_concurrency=args.max_concurrency,
            initial_concurrency=min(8, args.max_concurrency),
            enable_browser_escalation=not args.no_browser,
            timeout_seconds=settings.acquisition_timeout_seconds,
            max_artifact_bytes=settings.max_artifact_bytes,
            max_retries=settings.acquisition_retries + 1,
            storage_dir=Path(".data"),
        )
        sink = TerminalEventSink(verbose=args.verbose)

        with connection(settings.database_url) as conn:
            ledger = EvidenceLedger(conn)
            engine = AcquisitionEngine(engine_config, ledger, sink)

            interrupt_count = 0
            previous_handler = signal.getsignal(signal.SIGINT)

            def handle_sigint(signum, frame):
                nonlocal interrupt_count
                interrupt_count += 1
                if interrupt_count == 1:
                    engine.request_shutdown()
                    print(
                        "\n[interrupt] stopping — finishing in-flight work, no new requests will be scheduled "
                        "(press Ctrl+C again to force-quit)",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    signal.signal(signal.SIGINT, previous_handler)
                    if callable(previous_handler):
                        previous_handler(signum, frame)
                    else:
                        raise KeyboardInterrupt

            signal.signal(signal.SIGINT, handle_sigint)
            try:
                summary, run_id = engine.run(args.source_url)
            finally:
                signal.signal(signal.SIGINT, previous_handler)

        return _EXIT_CODE_BY_STATUS.get(summary.status, EXIT_FAILED)
    except (ApplicationError, ConfigurationError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR


if __name__ == "__main__":
    sys.exit(main())
