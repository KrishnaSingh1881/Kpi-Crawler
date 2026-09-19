"""End-to-end acquisition engine test against a real local HTTP server that
deliberately rate-limits and denies certain paths, so the adaptive
concurrency/backoff/retry machinery is exercised for real — not mocked.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest

from kpi_crawler.acquisition_engine.contract import AcquisitionState
from kpi_crawler.acquisition_engine.engine import AcquisitionEngine, EngineConfig
from kpi_crawler.acquisition_engine.events import (
    AttemptEvent,
    BackoffEvent,
    FinalSummaryEvent,
    ImportantEvent,
    ListEventSink,
    ProgressSnapshot,
)
from kpi_crawler.acquisition_engine.ledger import EvidenceLedger
from kpi_crawler.db import connection
from kpi_crawler.migrations import migrate

DATABASE_URL = os.environ.get("DATABASE_URL")

PAGES = {
    "/": b"""<html><body>
        <a href="/about">About</a>
        <a href="/flaky">Flaky</a>
        <a href="/denied">Denied</a>
        <a href="/ok2">Ok2</a>
        </body></html>""",
    "/about": b"<html><body>About page content</body></html>",
    "/ok2": b"<html><body>Second ok page</body></html>",
}


class _RateLimitedHandler(BaseHTTPRequestHandler):
    flaky_hits = 0
    lock = threading.Lock()

    def do_GET(self):
        if self.path == "/flaky":
            with _RateLimitedHandler.lock:
                _RateLimitedHandler.flaky_hits += 1
                hit = _RateLimitedHandler.flaky_hits
            if hit <= 2:
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b"<html><body>Finally succeeded</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/denied":
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        body = PAGES.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL integration tests")
class AcquisitionEngineHttpIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _RateLimitedHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def setUp(self):
        _RateLimitedHandler.flaky_hits = 0
        self.storage_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage_dir.cleanup)

    def _run(self, sink):
        config = EngineConfig(
            max_depth=1,
            max_artifacts=10,
            max_concurrency=4,
            initial_concurrency=4,
            max_retries=4,
            base_backoff_seconds=0.02,
            max_backoff_seconds=0.05,
            enable_browser_escalation=False,
            storage_dir=Path(self.storage_dir.name),
        )
        # Kept open (not `with connection(...)`) so the ledger can still be
        # queried by the test after the run completes; closed in cleanup.
        # The context manager object itself must be kept alive (not just its
        # __enter__ result) or it is garbage-collected immediately, which
        # closes the connection via the generator's own cleanup.
        cm = connection(DATABASE_URL)
        conn = cm.__enter__()
        self.addCleanup(lambda: cm.__exit__(None, None, None))
        ledger = EvidenceLedger(conn)
        engine = AcquisitionEngine(config, ledger, sink)
        return engine.run(self.base_url + "/"), ledger

    def test_run_is_partial_when_some_urls_succeed_and_one_is_denied(self):
        sink = ListEventSink()
        (summary, run_id), ledger = self._run(sink)

        self.assertEqual(summary.status, AcquisitionState.PARTIAL)
        self.assertGreaterEqual(summary.acquired, 3)  # /, /about, /ok2, and eventually /flaky
        self.assertGreaterEqual(summary.failed, 1)  # /denied never succeeds

    def test_flaky_endpoint_eventually_succeeds_after_retries_with_full_attempt_history(self):
        sink = ListEventSink()
        (summary, run_id), ledger = self._run(sink)

        attempts = ledger.attempts_for_run(run_id)
        flaky_attempts = [a for a in attempts if a.url.endswith("/flaky")]
        self.assertGreaterEqual(len(flaky_attempts), 3, "both 429 attempts and the eventual success must all be recorded")
        self.assertEqual(flaky_attempts[-1].final_result, AcquisitionState.SUCCESS)
        rate_limited = [a for a in flaky_attempts if a.final_result == AcquisitionState.RATE_LIMITED]
        self.assertEqual(len(rate_limited), 2, "the two 429 attempts must not be discarded or overwritten")

    def test_denied_endpoint_is_recorded_as_access_denied_not_silently_dropped(self):
        sink = ListEventSink()
        (summary, run_id), ledger = self._run(sink)

        attempts = ledger.attempts_for_run(run_id)
        denied_attempts = [a for a in attempts if a.url.endswith("/denied")]
        self.assertTrue(denied_attempts)
        self.assertTrue(all(a.final_result == AcquisitionState.ACCESS_DENIED for a in denied_attempts))

    def test_successful_artifacts_are_preserved_despite_partial_failures(self):
        sink = ListEventSink()
        (summary, run_id), ledger = self._run(sink)

        artifacts = ledger.artifacts_for_run(run_id)
        acquired_urls = {a.source_url for a in artifacts}
        self.assertIn(self.base_url + "/about", acquired_urls)
        self.assertIn(self.base_url + "/ok2", acquired_urls)
        self.assertNotIn(self.base_url + "/denied", acquired_urls, "no fake artifact for a failed request")

    def test_rate_limit_triggers_adaptive_decision_and_backoff_event(self):
        sink = ListEventSink()
        (summary, run_id), ledger = self._run(sink)

        important_headlines = [e.headline for e in sink.events if isinstance(e, ImportantEvent)]
        self.assertTrue(any("429" in h for h in important_headlines))
        self.assertTrue(any(isinstance(e, BackoffEvent) for e in sink.events), "terminal must show state during backoff, not go blank")

    def test_final_summary_event_is_always_emitted(self):
        sink = ListEventSink()
        (summary, run_id), ledger = self._run(sink)

        finals = [e for e in sink.events if isinstance(e, FinalSummaryEvent)]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].summary.run_id, run_id)

    def test_attempt_events_are_emitted_for_every_attempt(self):
        sink = ListEventSink()
        (summary, run_id), ledger = self._run(sink)

        attempt_events = [e for e in sink.events if isinstance(e, AttemptEvent)]
        ledger_attempts = ledger.attempts_for_run(run_id)
        self.assertEqual(len(attempt_events), len(ledger_attempts))

    def test_startup_events_appear_before_any_progress_or_result(self):
        """The terminal must show run start / target immediately — not only
        once the first periodic progress tick fires.
        """
        sink = ListEventSink()
        (summary, run_id), ledger = self._run(sink)

        important = [e for e in sink.events if isinstance(e, ImportantEvent)]
        self.assertTrue(any(e.icon == "🚀" for e in important), "must announce run start")
        self.assertTrue(any(self.base_url.split("//")[1] in e.headline for e in important), "must announce the target domain")
        self.assertLess(sink.events.index(next(e for e in important if e.icon == "🚀")), sink.events.index(next(e for e in sink.events if isinstance(e, FinalSummaryEvent))))

    def test_run_finished_status_event_is_emitted(self):
        sink = ListEventSink()
        (summary, run_id), ledger = self._run(sink)

        important = [e for e in sink.events if isinstance(e, ImportantEvent)]
        self.assertTrue(any("Run finished" in e.headline and summary.status.value in e.headline for e in important))

    def test_retry_budget_exhausted_event_for_persistently_denied_endpoint(self):
        sink = ListEventSink()
        (summary, run_id), ledger = self._run(sink)

        important = [e for e in sink.events if isinstance(e, ImportantEvent)]
        self.assertTrue(
            any("Retry budget exhausted" in e.headline and "/denied" in " ".join(e.detail_lines) for e in important)
        )

    def test_discovered_count_includes_links_never_attempted(self):
        """`/denied` and `/flaky` etc. are all same-host and within budget so
        they do get attempted, but `summary.discovered` must still be a
        distinct, correctly-tracked number (equal to attempted here, since
        this fixture has nothing off-host or over-budget) rather than a
        hardcoded/duplicated value that happens to look right by accident.
        """
        sink = ListEventSink()
        (summary, run_id), ledger = self._run(sink)
        self.assertGreaterEqual(summary.discovered, summary.attempted)
        self.assertEqual(summary.attempted, summary.acquired + summary.failed)

    def test_discovery_progress_event_is_emitted_once_per_wave_not_per_url(self):
        sink = ListEventSink()
        (summary, run_id), ledger = self._run(sink)

        discovery_events = [e for e in sink.events if isinstance(e, ImportantEvent) and e.icon == "🔎"]
        self.assertTrue(discovery_events, "must announce discovery progress")
        self.assertLess(
            len(discovery_events), summary.discovered,
            "discovery must be reported in aggregate per wave, not once per discovered URL",
        )

    def test_progress_ticker_fires_immediately_not_only_after_the_full_interval(self):
        """With a deliberately long interval, only an immediate first tick
        (not the periodic one) could appear during a run this short — proves
        the terminal is never blank waiting for the first periodic tick.
        """
        sink = ListEventSink()
        config = EngineConfig(
            max_depth=1, max_artifacts=10, max_concurrency=4, initial_concurrency=4,
            max_retries=4, base_backoff_seconds=0.02, max_backoff_seconds=0.05,
            enable_browser_escalation=False, storage_dir=Path(self.storage_dir.name),
            progress_interval_seconds=120.0,
        )
        cm = connection(DATABASE_URL)
        conn = cm.__enter__()
        self.addCleanup(lambda: cm.__exit__(None, None, None))
        ledger = EvidenceLedger(conn)
        engine = AcquisitionEngine(config, ledger, sink)
        engine.run(self.base_url + "/")

        progress_events = [e for e in sink.events if isinstance(e, ProgressSnapshot)]
        self.assertTrue(progress_events, "an immediate progress snapshot must fire even with a 120s periodic interval")


if __name__ == "__main__":
    unittest.main()
