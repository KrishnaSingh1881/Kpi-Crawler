"""Proves fingerprint management is real, not a stub, on the
PlaywrightCrawler-based path — replaces the pre-Crawlee
`browser_acquirer.BrowserManager`-based version of this test.

Crawlee 1.10.1's `PlaywrightCrawler` uses
`crawlee.fingerprint_suite.DefaultFingerprintGenerator` as its own default
fingerprint generator when none is given (confirmed by reading
`crawlers/_playwright/_playwright_crawler.py`), which this engine wires in
explicitly via `PlaywrightCrawler(fingerprint_generator=DefaultFingerprintGenerator())`.

One real behavioral difference from the old hand-rolled `_BrowserWorker`:
crawlee's own `BrowserPool` does *not* bind a specific browser context to a
specific `session_id` — pages share a rotating pool of browser contexts
(confirmed by reading `browsers/_browser_pool.py`), not one fixed context
per session the way `_BrowserWorker._context_for(session_id)` worked. So
this suite tests what the new architecture actually provides — fingerprinting
is genuinely active, and the on/off toggle works — rather than forcing the
old per-session-identity assertions onto a mechanism that no longer works
that way.
"""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest

from crawlee.fingerprint_suite import DefaultFingerprintGenerator

from kpi_crawler.acquisition_engine.engine import AcquisitionEngine, EngineConfig
from kpi_crawler.acquisition_engine.events import ListEventSink
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

# A page whose visible body is only populated by JS reading navigator.userAgent
# — reading the rendered content back out proves what the *browser itself*
# reported, not just what we independently asked the generator for. Two
# script tags (not one): unlike the old `BrowserManager`, which rendered via
# browser unconditionally, this engine tries HTTP first and only escalates
# if `dynamic_content_likely`'s script-byte-ratio threshold actually fires.
UA_PAGE = b"""<!DOCTYPE html><html><head>
<script src="/app.js"></script>
<script src="/vendor.js"></script>
</head><body><div id="ua"></div></body></html>"""

APP_JS = b"""
document.addEventListener('DOMContentLoaded', function () {
    document.getElementById('ua').innerText = 'UA:' + navigator.userAgent;
});
"""


class _UaHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body, content_type = UA_PAGE, "text/html"
        elif self.path == "/app.js":
            body, content_type = APP_JS, "application/javascript"
        elif self.path == "/vendor.js":
            body, content_type = b"// vendor bundle placeholder\n", "application/javascript"
        elif self.path in ("/robots.txt", "/sitemap.xml"):
            body, content_type = b"", "text/plain"
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


def _rendered_ua(content: bytes) -> str:
    text = content.decode("utf-8")
    marker = "UA:"
    start = text.index(marker) + len(marker)
    end = text.index("</div>", start)
    return text[start:end]


class FingerprintGeneratorUnitTests(unittest.TestCase):
    """No browser needed: proves the generator itself produces diverse, real
    profiles — the same property the old `BrowserManager`-based test proved
    indirectly through 8 separate browser sessions, now checked directly
    against the generator this engine actually wires into `PlaywrightCrawler`.
    """

    def test_generator_produces_diverse_profiles(self):
        generator = DefaultFingerprintGenerator()
        signatures = []
        for _ in range(8):
            fingerprint = generator.generate()
            self.assertTrue(fingerprint.navigator.userAgent)
            signatures.append((fingerprint.navigator.userAgent, fingerprint.screen.width, fingerprint.screen.height))
        self.assertGreater(
            len(set(signatures)), 1,
            "8 independently generated profiles should not all be identical",
        )


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL integration tests")
@unittest.skipUnless(PLAYWRIGHT_AVAILABLE, "Playwright/Chromium is not available in this environment")
class FingerprintWiringIntegrationTests(unittest.TestCase):
    """Drives a real headless Chromium instance through the engine's actual
    browser-escalation path against a real local HTTP server — nothing here
    is mocked.
    """

    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _UaHandler)
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

    def _rendered_root_ua(self, *, enable_fingerprinting: bool) -> str:
        config = EngineConfig(
            max_depth=0, max_artifacts=5, max_concurrency=2,
            enable_browser_escalation=True, browser_headless=True,
            enable_fingerprinting=enable_fingerprinting,
            storage_dir=Path(self.storage_dir.name),
        )
        sink = ListEventSink()
        cm = connection(DATABASE_URL)
        conn = cm.__enter__()
        self.addCleanup(lambda: cm.__exit__(None, None, None))
        ledger = EvidenceLedger(conn)
        engine = AcquisitionEngine(config, ledger, sink)
        summary, run_id = asyncio.run(engine.run(self.base_url + "/"))
        artifacts = ledger.artifacts_for_run(run_id)
        root_artifact = next(a for a in artifacts if a.source_url == self.base_url + "/")
        self.assertEqual(root_artifact.acquisition_method, "browser")
        return _rendered_ua(Path(root_artifact.raw_location).read_bytes())

    def test_fingerprinting_enabled_changes_the_reported_user_agent(self):
        """The rendered page must report a genuinely generated fingerprint's
        userAgent, not Playwright's own stock default — proven by comparing
        against the same site rendered with fingerprinting turned off.
        """
        fingerprinted_ua = self._rendered_root_ua(enable_fingerprinting=True)
        default_ua = self._rendered_root_ua(enable_fingerprinting=False)
        self.assertTrue(fingerprinted_ua)
        self.assertTrue(default_ua)
        self.assertNotEqual(
            fingerprinted_ua, default_ua,
            "fingerprinting must actually change what the browser reports, not just be present",
        )

    def test_fingerprinting_disabled_still_acquires_content_successfully(self):
        """Regression guard: the fingerprinting-off path must still
        successfully render and store content.
        """
        ua = self._rendered_root_ua(enable_fingerprinting=False)
        self.assertTrue(ua)


if __name__ == "__main__":
    unittest.main()
