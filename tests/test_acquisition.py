import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest

from kpi_crawler.acquisition import AcquisitionError, acquire_source
from kpi_crawler.errors import OperationalLimitError, StorageError


FIXTURE = Path(__file__).parent / "fixtures" / "known.pdf"


class Handler(BaseHTTPRequestHandler):
    retry_hits = 0
    missing_hits = 0

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/retry")
            self.end_headers()
        elif self.path == "/retry":
            type(self).retry_hits += 1
            if type(self).retry_hits == 1:
                self.send_response(503)
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"received")
        elif self.path == "/missing":
            type(self).missing_hits += 1
            self.send_response(404)
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(0.1)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"late")
            except BrokenPipeError:
                pass
        elif self.path == "/large":
            body = b"x" * 1000
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/large-no-length":
            body = b"x" * 1000
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(500)
            self.end_headers()

    def log_message(self, format, *args):
        pass


class AcquisitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def test_acquires_local_pdf_with_content_type_and_hash(self):
        with tempfile.TemporaryDirectory() as storage_dir:
            result = acquire_source(FIXTURE.resolve().as_uri(), Path(storage_dir))
            duplicate = acquire_source(FIXTURE.resolve().as_uri(), Path(storage_dir))
            self.assertEqual(result.source_type, "file")
            self.assertEqual(result.content_type, "application/pdf")
            self.assertEqual(result.sha256, hashlib.sha256(FIXTURE.read_bytes()).hexdigest())
            self.assertEqual(Path(result.raw_storage_ref).read_bytes(), FIXTURE.read_bytes())
            self.assertEqual(duplicate.raw_storage_ref, result.raw_storage_ref)

    def test_follows_redirects_and_retries_transient_http_failure(self):
        Handler.retry_hits = 0
        with tempfile.TemporaryDirectory() as storage_dir:
            result = acquire_source(f"{self.base_url}/redirect", Path(storage_dir), retries=1)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.resolved_url, f"{self.base_url}/retry")
            self.assertEqual(result.content_type, "text/plain")
            self.assertEqual(result.content, b"received")
            self.assertEqual(Handler.retry_hits, 2)

    def test_does_not_retry_not_found(self):
        Handler.missing_hits = 0
        with tempfile.TemporaryDirectory() as storage_dir:
            with self.assertRaisesRegex(AcquisitionError, "HTTP 404") as error:
                acquire_source(f"{self.base_url}/missing", Path(storage_dir), retries=2)
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(Handler.missing_hits, 1)

    def test_timeout_becomes_acquisition_error(self):
        with tempfile.TemporaryDirectory() as storage_dir:
            with self.assertRaises(AcquisitionError):
                acquire_source(
                    f"{self.base_url}/slow",
                    Path(storage_dir),
                    timeout_seconds=0.01,
                    retries=0,
                )

    def test_declared_content_length_over_limit_is_rejected_without_reading_body(self):
        with tempfile.TemporaryDirectory() as storage_dir:
            with self.assertRaisesRegex(OperationalLimitError, "exceeds the 100-byte limit"):
                acquire_source(f"{self.base_url}/large", Path(storage_dir), max_bytes=100)
            self.assertEqual(list(Path(storage_dir).iterdir()), [])

    def test_oversized_body_without_content_length_is_rejected(self):
        with tempfile.TemporaryDirectory() as storage_dir:
            with self.assertRaisesRegex(OperationalLimitError, "exceeds the 100-byte limit"):
                acquire_source(f"{self.base_url}/large-no-length", Path(storage_dir), max_bytes=100)
            self.assertEqual(list(Path(storage_dir).iterdir()), [])

    def test_oversized_local_file_is_rejected_without_reading_it(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as storage_dir:
            large_file = Path(source_dir) / "large.bin"
            large_file.write_bytes(b"x" * 1000)
            with self.assertRaisesRegex(OperationalLimitError, "exceeds the 100-byte limit"):
                acquire_source(large_file.resolve().as_uri(), Path(storage_dir), max_bytes=100)
            self.assertEqual(list(Path(storage_dir).iterdir()), [])

    def test_raw_storage_write_failure_becomes_storage_error(self):
        with tempfile.TemporaryDirectory() as parent:
            blocked_storage_dir = Path(parent) / "blocked"
            blocked_storage_dir.write_text("not a directory")
            with self.assertRaises(StorageError):
                acquire_source(FIXTURE.resolve().as_uri(), blocked_storage_dir)
