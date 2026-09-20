"""Proves graceful interruption: no fake SUCCESS, no corrupted evidence,
in-flight work finishes, no new work gets scheduled, and a real final
summary is always produced — against a real local server and a real engine
run (`AcquisitionEngine.request_shutdown()`, the same method the CLI's
SIGINT handler calls).

`request_shutdown()` delegates entirely to crawlee's own `Crawler.stop()`.
Its exact semantics (new task scheduling stops immediately; already-running
tasks are allowed to finish) were verified by reading crawlee 1.10.1's
`BasicCrawler.__is_task_ready_function`/`__is_finished_function` during the
Crawlee port (Stage 0), not assumed — these tests confirm that behavior
holds end-to-end through the engine.
"""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from kpi_crawler.acquisition_engine.contract import AcquisitionState
from kpi_crawler.acquisition_engine.engine import AcquisitionEngine, EngineConfig
from kpi_crawler.acquisition_engine.events import FinalSummaryEvent, ImportantEvent, ListEventSink
from kpi_crawler.acquisition_engine.ledger import EvidenceLedger
from kpi_crawler.db import connection
from kpi_crawler.migrations import migrate

DATABASE_URL = os.environ.get("DATABASE_URL")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = b'<html><body><a href="/slow">Slow</a><a href="/fast1">Fast</a></body></html>'
        elif self.path == "/slow":
            time.sleep(0.3)
            body = b'<html><body><a href="/child">Child</a></body></html>'
        elif self.path == "/fast1":
            body = b"<html><body>fast</body></html>"
        elif self.path == "/child":
            body = b"<html><body>should never be reached if interrupted before this wave</body></html>"
        else:
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
class InterruptionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
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

    def _engine(self, sink):
        config = EngineConfig(
            max_depth=2,
            max_artifacts=20,
            max_concurrency=4,
            max_retries=0,
            enable_browser_escalation=False,
            storage_dir=Path(self.storage_dir.name),
        )
        ledger = EvidenceLedger(self.conn)
        return AcquisitionEngine(config, ledger, sink), ledger

    def test_shutdown_requested_before_run_produces_interrupted_with_no_attempts(self):
        sink = ListEventSink()
        engine, ledger = self._engine(sink)
        engine.request_shutdown()

        summary, run_id = asyncio.run(engine.run(self.base_url + "/"))

        self.assertEqual(summary.status, AcquisitionState.INTERRUPTED)
        self.assertEqual(summary.attempted, 0)
        self.assertEqual(summary.acquired, 0)
        self.assertEqual(ledger.attempts_for_run(run_id), [])

        final_events = [e for e in sink.events if isinstance(e, FinalSummaryEvent)]
        self.assertEqual(len(final_events), 1, "a final summary must always be produced, even for an empty run")

    def test_shutdown_mid_run_finishes_in_flight_wave_but_schedules_no_new_work(self):
        sink = ListEventSink()
        engine, ledger = self._engine(sink)

        async def run_with_interrupt():
            async def interrupt_soon():
                await asyncio.sleep(0.1)  # well before /slow (0.3s) finishes
                engine.request_shutdown()

            interrupter = asyncio.create_task(interrupt_soon())
            result = await engine.run(self.base_url + "/")
            await interrupter
            return result

        summary, run_id = asyncio.run(run_with_interrupt())

        self.assertEqual(summary.status, AcquisitionState.INTERRUPTED)

        attempted_urls = {a.url for a in ledger.attempts_for_run(run_id)}
        self.assertIn(self.base_url + "/slow", attempted_urls, "in-flight work must be allowed to finish")
        self.assertIn(self.base_url + "/fast1", attempted_urls, "in-flight work must be allowed to finish")
        self.assertNotIn(
            self.base_url + "/child", attempted_urls,
            "no NEW work discovered after the interrupt should ever be scheduled",
        )

    def test_interrupted_status_is_persisted_in_the_ledger_not_just_in_memory(self):
        sink = ListEventSink()
        engine, ledger = self._engine(sink)
        engine.request_shutdown()
        summary, run_id = asyncio.run(engine.run(self.base_url + "/"))

        row = self.conn.execute("SELECT status FROM acq.runs WHERE id = %s", (run_id,)).fetchone()
        self.assertEqual(row[0], "INTERRUPTED")
        self.assertEqual(summary.status.value, "INTERRUPTED")

    def test_interrupted_run_never_silently_reports_success(self):
        sink = ListEventSink()
        engine, ledger = self._engine(sink)
        engine.request_shutdown()
        summary, run_id = asyncio.run(engine.run(self.base_url + "/"))

        self.assertNotEqual(summary.status, AcquisitionState.SUCCESS)
        final_event = next(e for e in sink.events if isinstance(e, FinalSummaryEvent))
        self.assertNotEqual(final_event.summary.status, AcquisitionState.SUCCESS)

    def test_run_finished_important_event_reflects_interrupted_status(self):
        sink = ListEventSink()
        engine, ledger = self._engine(sink)
        engine.request_shutdown()
        asyncio.run(engine.run(self.base_url + "/"))

        important = [e for e in sink.events if isinstance(e, ImportantEvent)]
        self.assertTrue(any("INTERRUPTED" in e.headline for e in important))


if __name__ == "__main__":
    unittest.main()
