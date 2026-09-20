"""End-to-end test of the Crawlee-backed `AcquisitionEngine` against a real
local HTTP server — replaces the pre-Crawlee `test_acquisition_engine_http_integration.py`.

Serves a small site with a 404 and a `<link rel="stylesheet">` reference,
and confirms:
  - a 404 becomes an `acq.attempts` row with `final_result=NOT_FOUND`
    (never NETWORK_ERROR), not a crash and not a retried request;
  - a stylesheet reference is discovered but never fetched — proving
    `discovery.discover_links`'s `is_resource_reference` filtering is what
    the engine's own link enqueueing runs through, not crawlee's
    `enqueue_links` (which has no notion of `is_resource_reference`);
  - the resulting `acq.attempts` rows and `artifacts_by_category` in the
    Program-2 manifest match exactly what the site actually contains.
"""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import asyncio
from pathlib import Path
import tempfile
import threading
import unittest

from kpi_crawler.acquisition_engine.contract import AcquisitionState
from kpi_crawler.acquisition_engine.engine import AcquisitionEngine, EngineConfig
from kpi_crawler.acquisition_engine.events import ListEventSink
from kpi_crawler.acquisition_engine.ledger import EvidenceLedger
from kpi_crawler.db import connection
from kpi_crawler.migrations import migrate

DATABASE_URL = os.environ.get("DATABASE_URL")

ROOT_BODY = (
    b'<html><head><link rel="stylesheet" href="/style.css"></head>'
    b'<body><a href="/child">Child</a><a href="/missing">Missing</a></body></html>'
)
CHILD_BODY = b"<html><body>child page, no further links</body></html>"


class _SiteHandler(BaseHTTPRequestHandler):
    style_hits = 0

    def do_GET(self):
        if self.path == "/":
            self._respond(200, ROOT_BODY, "text/html")
        elif self.path == "/child":
            self._respond(200, CHILD_BODY, "text/html")
        elif self.path in ("/robots.txt", "/sitemap.xml"):
            self._respond(200, b"", "text/plain")
        elif self.path == "/style.css":
            type(self).style_hits += 1
            self._respond(200, b"body { color: red; }", "text/css")
        else:
            self._respond(404, b"", "text/plain")

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL integration tests")
class HttpCrawlIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _SiteHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def setUp(self):
        _SiteHandler.style_hits = 0
        self.storage_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage_dir.cleanup)
        cm = connection(DATABASE_URL)
        self.conn = cm.__enter__()
        self.addCleanup(lambda: cm.__exit__(None, None, None))
        self.ledger = EvidenceLedger(self.conn)

    def _run(self):
        config = EngineConfig(
            max_depth=1,
            max_artifacts=20,
            max_concurrency=4,
            enable_browser_escalation=False,
            storage_dir=Path(self.storage_dir.name),
        )
        sink = ListEventSink()
        engine = AcquisitionEngine(config, self.ledger, sink)
        summary, run_id = asyncio.run(engine.run(self.base_url + "/"))
        return summary, run_id, engine

    def test_attempts_and_artifact_categories_match_the_site(self):
        summary, run_id, engine = self._run()

        attempts = self.ledger.attempts_for_run(run_id)
        by_url = {a.url: a for a in attempts}
        attempted_urls = set(by_url)

        expected_urls = {
            self.base_url + "/",
            self.base_url + "/robots.txt",
            self.base_url + "/sitemap.xml",
            self.base_url + "/child",
            self.base_url + "/missing",
        }
        self.assertEqual(attempted_urls, expected_urls)

        # The stylesheet is a resource reference, not a page: discovered but
        # never fetched, and the server must never have seen a request for it.
        self.assertNotIn(self.base_url + "/style.css", attempted_urls)
        self.assertEqual(_SiteHandler.style_hits, 0)

        # 404 must be classified NOT_FOUND, never NETWORK_ERROR, and never
        # retried (no_retry-on-client-error path, so retry_number stays 0).
        missing = by_url[self.base_url + "/missing"]
        self.assertEqual(missing.final_result, AcquisitionState.NOT_FOUND)
        self.assertEqual(missing.http_status, 404)
        self.assertEqual(missing.retry_number, 0)
        self.assertIsNone(missing.artifact_id)

        for url in (self.base_url + "/", self.base_url + "/child"):
            self.assertEqual(by_url[url].final_result, AcquisitionState.SUCCESS)
            self.assertEqual(by_url[url].http_status, 200)
            self.assertIsNotNone(by_url[url].artifact_id)

        self.assertEqual(summary.attempted, 5)
        self.assertEqual(summary.acquired, 4)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.status, AcquisitionState.PARTIAL)

        # artifacts_by_category, read back from the Program-2 manifest that
        # is this engine's actual on-disk output — two HTML pages, plus
        # robots.txt/sitemap.xml (fetched successfully, classified "other").
        run_dir = engine._run_storage.run_dir
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["artifacts_by_category"], {"html": 2, "other": 2})
        self.assertEqual(manifest["artifact_count"], 4)

    def test_child_page_content_is_stored_correctly(self):
        summary, run_id, engine = self._run()
        artifacts = self.ledger.artifacts_for_run(run_id)
        child_artifact = next(a for a in artifacts if a.source_url == self.base_url + "/child")
        self.assertEqual(Path(child_artifact.raw_location).read_bytes(), CHILD_BODY)
        self.assertEqual(child_artifact.discovered_from, self.base_url + "/")


if __name__ == "__main__":
    unittest.main()
