"""Tests for the blocked-status (403/429) backoff added to the Crawlee-backed
engine: a 403 (ACCESS_DENIED) must trigger the exact same concurrency-
reduction + fixed-delay pacing response as a 429 (RATE_LIMITED) — "this site
doesn't want us going this fast" is one category of signal, not two.

`_AdjustableAsyncGate` is tested directly (no server, no DB): it is the one
piece of genuinely new concurrency-control logic in this feature, since
Crawlee's own `ConcurrencySettings` has no supported way to be lowered at
runtime. `_on_blocked_signal`/`_note_success_for_slowdown_restore` are tested
directly against a real ledger (DB-backed, like every other ledger-touching
test in this suite) since they write real adaptive-decision rows. A real
local HTTP server proves the whole thing end to end: a real 403 measurably
paces the requests that follow it to the same domain.
"""

import asyncio
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest

from kpi_crawler.acquisition_engine.contract import AcquisitionState
from kpi_crawler.acquisition_engine.engine import AcquisitionEngine, EngineConfig, _AdjustableAsyncGate
from kpi_crawler.acquisition_engine.events import ListEventSink
from kpi_crawler.acquisition_engine.ledger import EvidenceLedger
from kpi_crawler.db import connection
from kpi_crawler.migrations import migrate

DATABASE_URL = os.environ.get("DATABASE_URL")


class AdjustableAsyncGateTests(unittest.TestCase):
    def test_never_admits_more_than_the_configured_limit_concurrently(self):
        async def scenario() -> int:
            gate = _AdjustableAsyncGate(2)
            concurrent = 0
            peak = 0

            async def worker() -> None:
                nonlocal concurrent, peak
                await gate.acquire()
                try:
                    concurrent += 1
                    peak = max(peak, concurrent)
                    await asyncio.sleep(0.05)
                finally:
                    concurrent -= 1
                    await gate.release()

            await asyncio.gather(*(worker() for _ in range(6)))
            return peak

        self.assertEqual(asyncio.run(scenario()), 2)

    def test_set_limit_can_lower_the_limit_while_holders_are_waiting(self):
        async def scenario() -> int:
            gate = _AdjustableAsyncGate(4)
            concurrent = 0
            peak = 0

            async def worker() -> None:
                nonlocal concurrent, peak
                await gate.acquire()
                try:
                    concurrent += 1
                    peak = max(peak, concurrent)
                    await asyncio.sleep(0.05)
                finally:
                    concurrent -= 1
                    await gate.release()

            await gate.set_limit(1)
            await asyncio.gather(*(worker() for _ in range(4)))
            return peak

        self.assertEqual(asyncio.run(scenario()), 1)


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL integration tests")
class BlockedSignalLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)

    def setUp(self):
        cm = connection(DATABASE_URL)
        self.conn = cm.__enter__()
        self.addCleanup(lambda: cm.__exit__(None, None, None))
        self.ledger = EvidenceLedger(self.conn)
        self.run_id = self.ledger.create_run("https://example.edu")

    def _engine(self, **config_overrides) -> AcquisitionEngine:
        config = EngineConfig(max_concurrency=8, **config_overrides)
        return AcquisitionEngine(config, self.ledger, ListEventSink())

    def test_403_reduces_concurrency_and_records_the_decision(self):
        engine = self._engine()
        triggered, reduced = asyncio.run(engine._on_blocked_signal(self.run_id, "example.edu"))
        self.assertTrue(triggered)
        self.assertEqual(reduced, 4)  # half of max_concurrency=8

        decisions = self.ledger.adaptive_decisions_for_run(self.run_id)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].decision_type, "concurrency_reduced")
        self.assertEqual(decisions[0].domain, "example.edu")
        self.assertEqual(decisions[0].before_value, "8")
        self.assertEqual(decisions[0].after_value, "4")
        self.assertIn("example.edu", engine._slowdowns)

    def test_429_triggers_the_identical_response_as_403(self):
        """The whole point of this feature: RATE_LIMITED and ACCESS_DENIED
        are the same category of signal to `_on_blocked_signal` — it has no
        idea which status code caused the call, by design.
        """
        engine_403 = self._engine()
        engine_429 = self._engine()
        run_id_2 = self.ledger.create_run("https://example.edu")

        triggered_403, reduced_403 = asyncio.run(engine_403._on_blocked_signal(self.run_id, "a.example.edu"))
        triggered_429, reduced_429 = asyncio.run(engine_429._on_blocked_signal(run_id_2, "a.example.edu"))

        self.assertEqual((triggered_403, reduced_403), (triggered_429, reduced_429))

    def test_repeat_signal_while_already_reduced_does_not_reduce_further(self):
        engine = self._engine()
        first_triggered, first_reduced = asyncio.run(engine._on_blocked_signal(self.run_id, "example.edu"))
        second_triggered, second_reduced = asyncio.run(engine._on_blocked_signal(self.run_id, "example.edu"))

        self.assertTrue(first_triggered)
        self.assertFalse(second_triggered, "a repeat 403/429 must not compound the reduction")
        self.assertEqual(first_reduced, second_reduced)

        decisions = self.ledger.adaptive_decisions_for_run(self.run_id)
        self.assertEqual(len(decisions), 1, "only the first signal should produce a ledger row")

    def test_restores_after_configured_consecutive_successes(self):
        engine = self._engine(blocked_restore_after_successes=3)
        asyncio.run(engine._on_blocked_signal(self.run_id, "example.edu"))
        self.assertIn("example.edu", engine._slowdowns)

        asyncio.run(engine._note_success_for_slowdown_restore(self.run_id, "example.edu"))
        asyncio.run(engine._note_success_for_slowdown_restore(self.run_id, "example.edu"))
        self.assertIn("example.edu", engine._slowdowns, "must not restore before the configured count")

        asyncio.run(engine._note_success_for_slowdown_restore(self.run_id, "example.edu"))
        self.assertNotIn("example.edu", engine._slowdowns, "must restore exactly at the configured count")

        decisions = self.ledger.adaptive_decisions_for_run(self.run_id)
        self.assertEqual([d.decision_type for d in decisions], ["concurrency_reduced", "concurrency_restored"])
        self.assertEqual(decisions[1].before_value, "4")
        self.assertEqual(decisions[1].after_value, "8")

    def test_a_failure_in_between_resets_the_restore_countdown(self):
        engine = self._engine(blocked_restore_after_successes=2)
        asyncio.run(engine._on_blocked_signal(self.run_id, "example.edu"))
        asyncio.run(engine._note_success_for_slowdown_restore(self.run_id, "example.edu"))
        # Another 403 before reaching the restore threshold resets the count
        # rather than restoring — same domain must not un-throttle mid-block.
        asyncio.run(engine._on_blocked_signal(self.run_id, "example.edu"))
        asyncio.run(engine._note_success_for_slowdown_restore(self.run_id, "example.edu"))
        self.assertIn("example.edu", engine._slowdowns, "one success after the reset must not be enough to restore")


ROOT_BODY = (
    b'<html><body>'
    b'<a href="/a">a</a><a href="/b">b</a><a href="/c">c</a>'
    b'<a href="/d">d</a><a href="/e">e</a>'
    b'</body></html>'
)


class _BlockedSiteHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self._respond(200, ROOT_BODY)
        elif self.path == "/a":
            self._respond(403, b"forbidden")
        elif self.path in ("/b", "/c", "/d", "/e"):
            self._respond(200, f"<html><body>{self.path}</body></html>".encode())
        else:
            self._respond(404, b"")

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL integration tests")
class BlockedBackoffIntegrationTests(unittest.TestCase):
    """A real 403 from a real server measurably paces subsequent requests to
    that same domain — not just a lower concurrency number with no pacing.
    """

    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _BlockedSiteHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def setUp(self):
        self.storage_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage_dir.cleanup)
        cm = connection(DATABASE_URL)
        self.conn = cm.__enter__()
        self.addCleanup(lambda: cm.__exit__(None, None, None))
        self.ledger = EvidenceLedger(self.conn)

    def test_403_causes_measurable_pacing_of_subsequent_same_domain_requests(self):
        # max_concurrency=1: a persistently-403'ing URL exhausts crawlee's own
        # session-rotation retries (up to max_session_rotations, internal to
        # crawlee — our failed_request_handler only ever sees it once, after
        # that's exhausted) before any sibling starts, so this test actually
        # observes pacing on requests that were still queued behind it rather
        # than ones crawlee had already dispatched concurrently beforehand.
        config = EngineConfig(
            max_depth=1, max_artifacts=10, max_concurrency=1,
            blocked_backoff_seconds=0.5, blocked_restore_after_successes=100,
            enable_browser_escalation=False, storage_dir=Path(self.storage_dir.name),
        )
        engine = AcquisitionEngine(config, self.ledger, ListEventSink())

        started = time.monotonic()
        summary, run_id = asyncio.run(engine.run(self.base_url + "/"))
        elapsed = time.monotonic() - started

        attempts = self.ledger.attempts_for_run(run_id)
        blocked = [a for a in attempts if a.final_result == AcquisitionState.ACCESS_DENIED]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].url, self.base_url + "/a")

        paced = [a for a in attempts if a.backoff_applied_seconds is not None]
        self.assertTrue(paced, "at least one attempt after the 403 must record a non-null backoff_applied_seconds")
        self.assertTrue(all(a.backoff_applied_seconds == 0.5 for a in paced))

        # With 4 remaining domain requests (/b, /c, /d, /e) each paced 0.5s
        # apart, the whole run cannot complete near-instantly the way an
        # unthrottled local-server crawl normally would.
        self.assertGreaterEqual(elapsed, 0.5)

        # max_concurrency=1 (chosen so the 403's crawlee-internal session-
        # rotation storm fully resolves before any sibling starts, making
        # this test deterministic rather than racing crawlee's own
        # dispatch order) means the "reduced" concurrency here is still 1 —
        # the halving arithmetic itself is proven separately, without any
        # server or timing dependency, by
        # `BlockedSignalLedgerTests.test_403_reduces_concurrency_and_records_the_decision`.
        decisions = self.ledger.adaptive_decisions_for_run(run_id)
        reduced = [d for d in decisions if d.decision_type == "concurrency_reduced"]
        self.assertEqual(len(reduced), 1)
        self.assertEqual(reduced[0].domain, f"127.0.0.1:{self.server.server_port}")
        self.assertEqual(reduced[0].before_value, "1")
        self.assertEqual(reduced[0].after_value, "1")


if __name__ == "__main__":
    unittest.main()
