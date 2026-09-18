from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest

import psycopg

from kpi_crawler.crawl import _normalize, run_crawl
from kpi_crawler.migrations import migrate
from kpi_crawler.storage import AcquisitionRepository


DATABASE_URL = os.environ.get("DATABASE_URL")


class NormalizeUrlTests(unittest.TestCase):
    """Pure function, no DB/network needed."""

    def test_scheme_and_host_case_are_folded(self):
        self.assertEqual(
            _normalize("HTTP://Example.COM/Path"),
            _normalize("http://example.com/Path"),
        )

    def test_path_case_is_preserved(self):
        self.assertNotEqual(_normalize("http://example.com/Path"), _normalize("http://example.com/path"))

    def test_fragment_is_stripped(self):
        self.assertEqual(_normalize("http://example.com/a#section"), _normalize("http://example.com/a"))

    def test_query_is_preserved(self):
        self.assertNotEqual(_normalize("http://example.com/a?x=1"), _normalize("http://example.com/a?x=2"))
# A distinct fixture (not known.pdf/structured.pdf) so its content-address never collides
# with another test module's artifact across separately torn-down temp storage directories.
PDF_FIXTURE = Path(__file__).parent / "fixtures" / "crawl_report.pdf"

PAGES = {
    "/": b"""
        <html><body>
        <a href="/about">About</a>
        <a href="/docs/">Docs</a>
        <a href="/report.pdf">Report</a>
        <a href="/">Home again</a>
        <a href="http://off-host.invalid/elsewhere">Off host</a>
        </body></html>
    """,
    "/about": b"""
        <html><body>
        <a href="/">Home</a>
        <a href="/docs/">Docs</a>
        </body></html>
    """,
    "/docs/": b'<html><body><a href="/docs/page1">Page 1</a></body></html>',
    "/docs/page1": b"""
        <html><head><link rel="next" href="/docs/page2"></head>
        <body>Page 1 content</body></html>
    """,
    "/docs/page2": b"<html><body>Page 2 content, no further pagination</body></html>",
    "/sitemap-only.html": b"<html><body>Only reachable via the sitemap</body></html>",
    "/robots.txt": b"User-agent: *\nSitemap: {sitemap_url}\n",
    "/sitemap.xml": (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>{sitemap_only_url}</loc></url>"
        b"</urlset>"
    ),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/report.pdf":
            body = PDF_FIXTURE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        template = PAGES.get(self.path)
        if template is None:
            self.send_response(404)
            self.end_headers()
            return

        origin = f"http://{self.headers.get('Host')}"
        body = template.replace(b"{sitemap_url}", f"{origin}/sitemap.xml".encode()).replace(
            b"{sitemap_only_url}", f"{origin}/sitemap-only.html".encode()
        )
        content_type = "application/xml" if self.path == "/sitemap.xml" else (
            "text/plain" if self.path == "/robots.txt" else "text/html"
        )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL integration tests")
class CrawlIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)
        cls.storage_dir = tempfile.TemporaryDirectory()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()
        cls.storage_dir.cleanup()

    def _acquired_urls(self, result):
        return {page.source_url for page in result.pages if page.artifact is not None}

    def test_discovers_nested_pages_resources_and_sitemap_only_page(self):
        result = run_crawl(self.base_url + "/", DATABASE_URL, Path(self.storage_dir.name), max_depth=3)

        acquired = self._acquired_urls(result)
        self.assertIn(self.base_url + "/", acquired)
        self.assertIn(self.base_url + "/about", acquired)
        self.assertIn(self.base_url + "/docs/", acquired)
        self.assertIn(self.base_url + "/docs/page1", acquired)
        self.assertIn(self.base_url + "/report.pdf", acquired)
        self.assertIn(self.base_url + "/sitemap-only.html", acquired, "sitemap discovery must reach an otherwise-unlinked page")

        report_page = next(page for page in result.pages if page.source_url == self.base_url + "/report.pdf")
        self.assertTrue(report_page.evidence, "the downloadable PDF resource must be extracted like any other PDF")
        self.assertIn("KPI", report_page.evidence[0].evidence.claim)
        self.assertIn("99", report_page.evidence[0].evidence.value)

        # The home page is linked to from itself and appears once in the discovery seed list;
        # deduplication must still leave exactly one page entry for it.
        home_entries = [page for page in result.pages if page.source_url == self.base_url + "/"]
        self.assertEqual(len(home_entries), 1)

        self.assertEqual(result.discovered, result.acquired + result.rejected)

    def test_lineage_links_a_discovered_page_to_the_page_that_found_it(self):
        run_crawl(self.base_url + "/", DATABASE_URL, Path(self.storage_dir.name), max_depth=3)

        with psycopg.connect(DATABASE_URL) as conn:
            docs_event = conn.execute(
                "SELECT id FROM app.acquisition_events WHERE source_url = %s AND status = 'succeeded' ORDER BY id DESC LIMIT 1",
                (self.base_url + "/docs/",),
            ).fetchone()
            page1_event = conn.execute(
                "SELECT parent_event_id FROM app.acquisition_events WHERE source_url = %s AND status = 'succeeded' ORDER BY id DESC LIMIT 1",
                (self.base_url + "/docs/page1",),
            ).fetchone()
        self.assertIsNotNone(docs_event)
        self.assertIsNotNone(page1_event)
        self.assertEqual(page1_event[0], docs_event[0])

    def test_pagination_link_is_followed_past_the_structural_depth_limit(self):
        # root(0) -> /docs/(1) -> /docs/page1(2); max_depth=2 lets page1 in, and its
        # rel="next" link to page2 must stay at depth 2 (pagination, not a deeper page).
        result = run_crawl(self.base_url + "/", DATABASE_URL, Path(self.storage_dir.name), max_depth=2)
        acquired = self._acquired_urls(result)
        self.assertIn(self.base_url + "/docs/page1", acquired)
        self.assertIn(self.base_url + "/docs/page2", acquired)

    def test_ordinary_nested_links_respect_the_depth_limit(self):
        # root(0) -> /docs/(1); with max_depth=1, /docs/page1 (an ordinary link at depth 2)
        # must not be followed.
        result = run_crawl(self.base_url + "/", DATABASE_URL, Path(self.storage_dir.name), max_depth=1)
        acquired = self._acquired_urls(result)
        self.assertIn(self.base_url + "/docs/", acquired)
        self.assertNotIn(self.base_url + "/docs/page1", acquired)
        self.assertNotIn(self.base_url + "/docs/page2", acquired)

    def test_off_host_link_is_rejected_without_being_fetched(self):
        result = run_crawl(self.base_url + "/", DATABASE_URL, Path(self.storage_dir.name), max_depth=3)
        self.assertFalse(any("off-host.invalid" in page.source_url for page in result.pages))

        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute(
                """
                SELECT status, error_message FROM app.acquisition_events
                WHERE source_url = 'http://off-host.invalid/elsewhere'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "failed")
        self.assertIn("off-host", row[1])

    def test_max_artifacts_limit_stops_acquisition_and_is_recorded(self):
        result = run_crawl(
            self.base_url + "/", DATABASE_URL, Path(self.storage_dir.name), max_depth=3, max_artifacts=2
        )
        self.assertLessEqual(result.acquired, 2)

        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute(
                """
                SELECT status, error_message FROM app.acquisition_events
                WHERE run_id = %s AND event_type = 'discovery' AND error_message LIKE 'max artifacts%%'
                LIMIT 1
                """,
                (result.run_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "failed")


if __name__ == "__main__":
    unittest.main()
