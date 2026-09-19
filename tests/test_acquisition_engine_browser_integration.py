"""End-to-end browser-escalation test: a real local page whose HTTP body is a
near-empty script-only shell, and whose actual content only exists after
client-side JS runs. Proves the engine really detects this, really launches
Playwright/Chromium, and really gets the rendered content — not mocked.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest

from kpi_crawler.acquisition_engine.contract import AcquisitionState
from kpi_crawler.acquisition_engine.engine import AcquisitionEngine, EngineConfig
from kpi_crawler.acquisition_engine.events import ImportantEvent, ListEventSink
from kpi_crawler.acquisition_engine.ledger import EvidenceLedger
from kpi_crawler.db import connection
from kpi_crawler.migrations import migrate

DATABASE_URL = os.environ.get("DATABASE_URL")


def _playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


PLAYWRIGHT_AVAILABLE = _playwright_available()

SPA_SHELL = b"""<!DOCTYPE html><html><head>
<script src="/app.js"></script>
<script src="/vendor.js"></script>
</head><body><div id="root"></div></body></html>"""

APP_JS = b"""
document.addEventListener('DOMContentLoaded', function () {
    document.getElementById('root').innerText = 'Rendered institutional KPI data: 4201 students enrolled';
});
"""


class _SpaHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = SPA_SHELL
            content_type = "text/html"
        elif self.path in ("/app.js", "/vendor.js"):
            body = APP_JS if self.path == "/app.js" else b"// vendor bundle placeholder\n"
            content_type = "application/javascript"
        elif self.path in ("/robots.txt", "/sitemap.xml"):
            # The engine always seeds these two well-known paths; keep them
            # trivially successful so this test isolates escalation behavior.
            body = b""
            content_type = "text/plain"
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL integration tests")
@unittest.skipUnless(PLAYWRIGHT_AVAILABLE, "Playwright/Chromium is not available in this environment")
class BrowserEscalationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _SpaHandler)
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

    def test_dynamic_content_is_escalated_to_browser_and_rendered_content_is_stored(self):
        config = EngineConfig(
            max_depth=0,
            max_artifacts=5,
            max_concurrency=2,
            initial_concurrency=2,
            max_retries=1,
            enable_browser_escalation=True,
            browser_headless=True,
            storage_dir=Path(self.storage_dir.name),
        )
        sink = ListEventSink()
        cm = connection(DATABASE_URL)
        conn = cm.__enter__()
        self.addCleanup(lambda: cm.__exit__(None, None, None))
        ledger = EvidenceLedger(conn)
        engine = AcquisitionEngine(config, ledger, sink)

        summary, run_id = engine.run(self.base_url + "/")

        self.assertEqual(summary.status, AcquisitionState.SUCCESS)
        self.assertGreaterEqual(summary.browser_pages, 1)

        artifacts = ledger.artifacts_for_run(run_id)
        root_artifact = next(a for a in artifacts if a.source_url == self.base_url + "/")
        self.assertEqual(root_artifact.acquisition_method, "browser")
        rendered_bytes = Path(root_artifact.raw_location).read_bytes()
        self.assertIn(b"Rendered institutional KPI data", rendered_bytes)

        attempts = ledger.attempts_for_run(run_id)
        root_attempts = [a for a in attempts if a.url == self.base_url + "/"]
        self.assertEqual(len(root_attempts), 2, "the HTTP shell fetch and the browser fetch must both be recorded")
        self.assertEqual(root_attempts[0].acquisition_method, "http")
        self.assertEqual(root_attempts[0].final_result, AcquisitionState.PENDING_RETRY)
        self.assertEqual(root_attempts[1].acquisition_method, "browser")
        self.assertEqual(root_attempts[1].final_result, AcquisitionState.SUCCESS)

        escalation_events = [e for e in sink.events if isinstance(e, ImportantEvent) and "Dynamic content" in e.headline]
        self.assertTrue(escalation_events)


if __name__ == "__main__":
    unittest.main()
