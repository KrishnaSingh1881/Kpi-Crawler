import io
from datetime import datetime, timezone
import unittest

from kpi_crawler.acquisition_engine.cli import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_PARTIAL,
    EXIT_SUCCESS,
    TerminalEventSink,
    _EXIT_CODE_BY_STATUS,
    _format_final_summary,
    _redact,
)
from kpi_crawler.acquisition_engine.contract import AcquisitionState, AttemptRecord, RunSummary
from kpi_crawler.acquisition_engine.events import AttemptEvent, BackoffEvent, FinalSummaryEvent, ImportantEvent, ProgressSnapshot

NOW = datetime(2026, 9, 19, 9, 31, 12, tzinfo=timezone.utc)


def _attempt(**overrides) -> AttemptRecord:
    defaults = dict(
        run_id=1, attempt_id=1, occurred_at=NOW, url="https://example.edu/report",
        domain="example.edu", acquisition_method="http", session_id="session_1",
        proxy_id=None, proxy_status=None, http_status=429, latency_ms=120.0,
        retry_number=3, retry_budget=5, timeout_seconds=10.0, backoff_applied_seconds=30.0,
        concurrency_at_attempt=4, rate_limit_detected=True, failure_classification="http_429",
        adaptive_decision="concurrency 8 -> 4", final_result=AcquisitionState.RATE_LIMITED,
        artifact_id=None, error_message=None,
    )
    defaults.update(overrides)
    return AttemptRecord(**defaults)


def _summary(status: AcquisitionState, **overrides) -> RunSummary:
    defaults = dict(
        run_id=1, contract_version=1, root_source_url="https://example.edu", started_at=NOW,
        completed_at=NOW, status=status, discovered=190, attempted=184, acquired=171, partial=0,
        failed=6, retries=13, rate_limited=2, browser_pages=24, proxy_failures=1,
        by_state={"SUCCESS": 171, "RATE_LIMITED": 4, "ACCESS_DENIED": 2},
    )
    defaults.update(overrides)
    return RunSummary(**defaults)


class RedactionTests(unittest.TestCase):
    def test_strips_credentials_from_proxy_url(self):
        text = "connecting via http://operator:s3cr3t@proxy.internal:8080 failed"
        redacted = _redact(text)
        self.assertNotIn("operator", redacted)
        self.assertNotIn("s3cr3t", redacted)
        self.assertIn("http://proxy.internal:8080", redacted)

    def test_leaves_ordinary_urls_untouched(self):
        text = "https://example.edu/report?x=1"
        self.assertEqual(_redact(text), text)

    def test_important_event_headline_and_details_are_redacted(self):
        buf = io.StringIO()
        sink = TerminalEventSink(verbose=False, stream=buf)
        sink.emit(
            ImportantEvent(
                at=NOW, icon="⚠", headline="proxy http://user:hunter2@proxy.local unhealthy",
                detail_lines=("→ retrying via http://user:hunter2@proxy.local",),
            )
        )
        output = buf.getvalue()
        self.assertNotIn("hunter2", output)
        self.assertNotIn("user:hunter2", output)

    def test_verbose_attempt_error_message_is_redacted(self):
        buf = io.StringIO()
        sink = TerminalEventSink(verbose=True, stream=buf)
        attempt = _attempt(error_message="failed to connect via http://user:hunter2@proxy.local:8080")
        sink.emit(AttemptEvent(attempt=attempt))
        output = buf.getvalue()
        self.assertNotIn("hunter2", output)


class TerminalEventSinkTests(unittest.TestCase):
    def test_normal_mode_hides_attempt_events(self):
        buf = io.StringIO()
        sink = TerminalEventSink(verbose=False, stream=buf)
        sink.emit(AttemptEvent(attempt=_attempt()))
        self.assertEqual(buf.getvalue(), "")

    def test_verbose_mode_shows_structured_attempt_fields(self):
        buf = io.StringIO()
        sink = TerminalEventSink(verbose=True, stream=buf)
        sink.emit(AttemptEvent(attempt=_attempt(), fingerprint_summary="Win32:abcd1234"))
        output = buf.getvalue()
        self.assertIn("[attempt] id=att_1", output)
        self.assertIn("[url] https://example.edu/report", output)
        self.assertIn("[method] http", output)
        self.assertIn("[session] session_1", output)
        self.assertIn("[fingerprint] Win32:abcd1234", output)
        self.assertIn("[status] 429", output)
        self.assertIn("[retry] 3/5", output)
        self.assertIn("[backoff] 30s", output)
        self.assertIn("[policy] concurrency 8 -> 4", output)
        self.assertIn("[evidence] recorded", output)
        self.assertIn("[result] RATE_LIMITED", output)

    def test_verbose_mode_shows_artifact_reference_when_present(self):
        buf = io.StringIO()
        sink = TerminalEventSink(verbose=True, stream=buf)
        sink.emit(AttemptEvent(attempt=_attempt(artifact_id=42, final_result=AcquisitionState.SUCCESS)))
        self.assertIn("[artifact] 42", buf.getvalue())

    def test_verbose_mode_omits_fingerprint_line_when_none(self):
        buf = io.StringIO()
        sink = TerminalEventSink(verbose=True, stream=buf)
        sink.emit(AttemptEvent(attempt=_attempt()))  # no fingerprint_summary
        self.assertNotIn("[fingerprint]", buf.getvalue())

    def test_progress_snapshot_shows_counts_and_rate(self):
        buf = io.StringIO()
        sink = TerminalEventSink(verbose=False, stream=buf)
        sink.emit(
            ProgressSnapshot(
                at=NOW, elapsed_seconds=30.0, current_domain="example.edu", discovered=184,
                processed=71, acquired=71, partial=0, failed=0, retries=0, concurrency=8,
                request_rate_per_s=2.3, active_browser_sessions=0,
            )
        )
        output = buf.getvalue()
        self.assertIn("71/184 acquired", output)
        self.assertIn("concurrency=8", output)
        self.assertIn("rate=2.3/s", output)

    def test_important_event_always_shown(self):
        buf = io.StringIO()
        sink = TerminalEventSink(verbose=False, stream=buf)
        sink.emit(ImportantEvent(at=NOW, icon="⚠", headline="429 detected", detail_lines=("→ Reducing concurrency 8 → 4",)))
        output = buf.getvalue()
        self.assertIn("429 detected", output)
        self.assertIn("Reducing concurrency", output)

    def test_backoff_event_shows_state_not_blank(self):
        buf = io.StringIO()
        sink = TerminalEventSink(verbose=False, stream=buf)
        sink.emit(BackoffEvent(at=NOW, seconds=8.0, reason="RATE_LIMITED", domain="example.edu", concurrency=4))
        output = buf.getvalue()
        self.assertIn("Backing off 8s", output)
        self.assertIn("example.edu", output)
        self.assertIn("Concurrency: 4", output)

    def test_final_summary_always_shown_and_has_all_fields(self):
        buf = io.StringIO()
        sink = TerminalEventSink(verbose=False, stream=buf)
        sink.emit(FinalSummaryEvent(summary=_summary(AcquisitionState.PARTIAL), evidence_path="./runs/8f31c2/"))
        output = buf.getvalue()
        for expected in ("Run ID          : 1", "example.edu", "190", "184", "171", "13", "2", "24", "1", "./runs/8f31c2/", "PARTIAL"):
            self.assertIn(expected, output)

    def test_final_summary_emitted_even_for_complete_failure(self):
        buf = io.StringIO()
        sink = TerminalEventSink(verbose=False, stream=buf)
        summary = _summary(AcquisitionState.FAILED, attempted=5, acquired=0, failed=5, discovered=5, by_state={"NETWORK_ERROR": 5})
        sink.emit(FinalSummaryEvent(summary=summary, evidence_path="./runs/dead/"))
        output = buf.getvalue()
        self.assertIn("ACQUISITION RESULT", output)
        self.assertIn("Status          : FAILED", output)

    def test_non_interactive_stream_receives_identical_plain_text(self):
        """No ANSI/cursor codes anywhere — a StringIO (no isatty) must render
        byte-identically to what a real terminal would show.
        """
        buf = io.StringIO()
        sink = TerminalEventSink(verbose=False, stream=buf)
        sink.emit(ImportantEvent(at=NOW, icon="🚀", headline="Starting acquisition"))
        output = buf.getvalue()
        self.assertNotIn("\x1b[", output)
        self.assertIn("Starting acquisition", output)


class FailureSummaryTests(unittest.TestCase):
    def test_no_failure_breakdown_for_clean_success(self):
        text = _format_final_summary(FinalSummaryEvent(summary=_summary(AcquisitionState.SUCCESS), evidence_path="./runs/x/"))
        self.assertNotIn("Failures:", text)

    def test_partial_run_lists_failure_breakdown_from_by_state(self):
        summary = _summary(AcquisitionState.PARTIAL, by_state={"SUCCESS": 171, "RATE_LIMITED": 7, "ACCESS_DENIED": 4, "TIMEOUT": 2})
        event = FinalSummaryEvent(summary=summary, evidence_path="./runs/8f31c2/", adaptive_action_counts={"concurrency_reduced": 1})
        text = _format_final_summary(event)
        self.assertIn("Failures:", text)
        self.assertIn("- 7 RATE_LIMITED", text)
        self.assertIn("- 4 ACCESS_DENIED", text)
        self.assertIn("- 2 TIMEOUT", text)
        self.assertIn("Adaptive actions:", text)
        self.assertIn("concurrency reduced (1x)", text)
        self.assertIn("13 retries performed", text)

    def test_complete_failure_still_lists_breakdown(self):
        summary = _summary(AcquisitionState.FAILED, attempted=5, acquired=0, failed=5, discovered=5, by_state={"NETWORK_ERROR": 5})
        event = FinalSummaryEvent(summary=summary, evidence_path="./runs/dead/")
        text = _format_final_summary(event)
        self.assertIn("- 5 NETWORK_ERROR", text)

    def test_failure_breakdown_does_not_double_count_success(self):
        summary = _summary(AcquisitionState.PARTIAL, by_state={"SUCCESS": 171, "RATE_LIMITED": 7})
        event = FinalSummaryEvent(summary=summary, evidence_path="./runs/x/")
        text = _format_final_summary(event)
        self.assertNotIn("SUCCESS", text.split("Failures:")[1].split("Adaptive")[0])


class ExitCodeMappingTests(unittest.TestCase):
    def test_each_status_maps_to_a_distinct_meaningful_code(self):
        codes = {
            AcquisitionState.SUCCESS: EXIT_SUCCESS,
            AcquisitionState.PARTIAL: EXIT_PARTIAL,
            AcquisitionState.FAILED: EXIT_FAILED,
            AcquisitionState.INTERRUPTED: EXIT_INTERRUPTED,
        }
        for status, expected in codes.items():
            self.assertEqual(_EXIT_CODE_BY_STATUS[status], expected)
        self.assertEqual(len(set(codes.values())), 4, "exit codes must be distinguishable from each other")
        self.assertNotEqual(EXIT_SUCCESS, EXIT_CONFIG_ERROR)


class ConsistentStatisticsTests(unittest.TestCase):
    def test_attempted_equals_acquired_plus_failed(self):
        """Guards against the exact bug class this task calls out: two
        independently-tracked numbers silently drifting apart.
        """
        summary = _summary(AcquisitionState.PARTIAL, attempted=184, acquired=171, failed=13)
        self.assertEqual(summary.attempted, summary.acquired + summary.failed)

    def test_discovered_is_never_less_than_attempted(self):
        summary = _summary(AcquisitionState.PARTIAL, discovered=190, attempted=184)
        self.assertGreaterEqual(summary.discovered, summary.attempted)


class FinalSummaryFormatTests(unittest.TestCase):
    def test_format_includes_status_line(self):
        text = _format_final_summary(FinalSummaryEvent(summary=_summary(AcquisitionState.FAILED), evidence_path="./runs/x/"))
        self.assertIn("Status          : FAILED", text)
        self.assertIn("ACQUISITION RESULT", text)


if __name__ == "__main__":
    unittest.main()
