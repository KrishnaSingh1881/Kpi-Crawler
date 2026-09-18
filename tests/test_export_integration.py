from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

import psycopg

from kpi_crawler.crawl import run_crawl
from kpi_crawler.errors import ExportError
from kpi_crawler.export import export_run
from kpi_crawler.migrations import migrate
from kpi_crawler.pipeline import run_pdf_pipeline
from kpi_crawler.storage import AcquisitionRepository


DATABASE_URL = os.environ.get("DATABASE_URL")
PDF_FIXTURE = Path(__file__).parent / "fixtures" / "export_report.pdf"


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
        if self.path == "/":
            body = (
                b'<html><body><a href="/report.pdf">Report</a>'
                b'<a href="/notes.txt">Notes</a>'
                b'<a href="/missing.pdf">Missing</a></body></html>'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/notes.txt":
            body = b"Plain text notes, not a PDF."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL integration tests")
class ExportCrawlRunTests(unittest.TestCase):
    """One shared crawl run (multiple sources, one with no evidence, one rejection) for most assertions."""

    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)
        cls.storage_dir = tempfile.TemporaryDirectory()
        cls.export_root = tempfile.TemporaryDirectory()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.crawl_result = run_crawl(
            cls.base_url + "/", DATABASE_URL, Path(cls.storage_dir.name), max_depth=1, same_host_only=False
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()
        cls.storage_dir.cleanup()
        cls.export_root.cleanup()

    def _export(self):
        return export_run(self.crawl_result.run_id, DATABASE_URL, Path(self.export_root.name))

    def _pdf_source_dir(self, result):
        with psycopg.connect(DATABASE_URL) as conn:
            sha256 = hashlib.sha256(PDF_FIXTURE.read_bytes()).hexdigest()
            artifact = AcquisitionRepository(conn).find_artifact(sha256)
        return result.output_dir / "sources" / str(artifact.id), artifact

    def test_creates_expected_directory_structure(self):
        result = self._export()
        self.assertTrue((result.output_dir / "README.md").is_file())
        self.assertTrue((result.output_dir / "manifest.json").is_file())
        self.assertTrue((result.output_dir / "sources").is_dir())

    def test_multiple_sources_are_exported(self):
        result = self._export()
        # The home page itself, report.pdf, and notes.txt were all acquired (robots.txt/sitemap.xml
        # 404 on this test server and are rejected, not sources).
        self.assertEqual(result.source_count, 3)
        self.assertEqual(len(list((result.output_dir / "sources").iterdir())), 3)

    def test_raw_artifact_is_byte_identical(self):
        result = self._export()
        source_dir, artifact = self._pdf_source_dir(result)
        exported_bytes = (source_dir / "original.pdf").read_bytes()
        self.assertEqual(exported_bytes, PDF_FIXTURE.read_bytes())

    def test_evidence_jsonl_matches_persisted_evidence(self):
        result = self._export()
        source_dir, artifact = self._pdf_source_dir(result)

        with psycopg.connect(DATABASE_URL) as conn:
            db_evidence = AcquisitionRepository(conn).list_evidence_for_artifact(artifact.id)
        self.assertTrue(db_evidence)

        lines = (source_dir / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(db_evidence))
        exported = [json.loads(line) for line in lines]
        first_exported = exported[0]
        first_db = db_evidence[0]

        self.assertEqual(first_exported["evidence_id"], first_db.evidence.id)
        self.assertEqual(first_exported["artifact_id"], first_db.evidence.artifact_id)
        self.assertEqual(first_exported["evidence_type"], first_db.evidence.evidence_type)
        self.assertEqual(first_exported["claim"], first_db.evidence.claim)
        self.assertEqual(first_exported["value"], first_db.evidence.value)
        self.assertEqual(first_exported["supporting_context"], first_db.evidence.supporting_context)
        self.assertEqual(first_exported["structure"], first_db.evidence.structure)
        self.assertEqual(first_exported["canonical_text"], first_db.evidence.canonical_text)
        self.assertEqual(first_exported["canonical_serializer_version"], first_db.evidence.canonical_serializer_version)
        self.assertEqual(first_exported["source_url"], first_db.source_url)
        self.assertEqual(first_exported["page_number"], first_db.page_number)
        self.assertEqual(first_exported["location"], first_db.location)
        self.assertEqual(first_exported["extractor"], first_db.extractor)
        self.assertEqual(first_exported["extractor_version"], first_db.extractor_version)

    def test_provenance_is_traceable_from_source_url_through_evidence(self):
        result = self._export()
        source_dir, artifact = self._pdf_source_dir(result)
        source_json = json.loads((source_dir / "source.json").read_text(encoding="utf-8"))
        source_url = source_json["source_urls"][0]["source_url"]
        self.assertEqual(source_url, self.base_url + "/report.pdf")

        evidence_line = (source_dir / "evidence.jsonl").read_text(encoding="utf-8").splitlines()[0]
        record = json.loads(evidence_line)
        self.assertEqual(record["source_url"], source_url)
        self.assertEqual(record["artifact_id"], artifact.id)
        self.assertEqual(record["page_number"], 1)
        self.assertIsNotNone(record["location"])

    def test_source_with_no_evidence_still_gets_a_directory(self):
        result = self._export()
        with psycopg.connect(DATABASE_URL) as conn:
            notes_artifact = AcquisitionRepository(conn).find_artifact(
                hashlib.sha256(b"Plain text notes, not a PDF.").hexdigest()
            )
        self.assertIsNotNone(notes_artifact)
        source_dir = result.output_dir / "sources" / str(notes_artifact.id)
        self.assertTrue(source_dir.is_dir())
        self.assertEqual((source_dir / "evidence.jsonl").read_text(encoding="utf-8"), "")
        source_json = json.loads((source_dir / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(source_json["evidence_count"], 0)

    def test_failed_acquisition_is_listed_as_rejected_not_a_source(self):
        result = self._export()
        manifest = json.loads((result.output_dir / "manifest.json").read_text(encoding="utf-8"))
        missing_url = self.base_url + "/missing.pdf"

        rejected_urls = {entry["source_url"] for entry in manifest["rejected"]}
        self.assertIn(missing_url, rejected_urls)

        # No artifact was ever created for the 404, so it cannot appear under any source's URLs.
        all_source_urls = {url for entry in manifest["sources"] for url in entry["source_urls"]}
        self.assertNotIn(missing_url, all_source_urls)

    def test_readme_contains_required_information(self):
        result = self._export()
        readme = (result.output_dir / "README.md").read_text(encoding="utf-8")
        self.assertIn(self.base_url + "/", readme)
        self.assertIn(f"Run ID: {self.crawl_result.run_id}", readme)
        self.assertIn("Resources discovered", readme)
        self.assertIn("Resources acquired", readme)
        self.assertIn("rejected", readme.lower())
        self.assertIn("application/pdf", readme)
        self.assertIn("Total evidence records", readme)
        self.assertIn("evidence record is one fact", readme.lower())
        self.assertIn("traced back", readme.lower())
        self.assertIn("Sources included", readme)

    def test_manifest_contains_run_and_source_metadata(self):
        result = self._export()
        manifest = json.loads((result.output_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["run"]["id"], self.crawl_result.run_id)
        self.assertEqual(manifest["run"]["root_source_url"], self.base_url + "/")
        self.assertIn(manifest["run"]["status"], {"succeeded", "failed"})
        self.assertEqual(manifest["summary"]["evidence_total"], sum(s["evidence_count"] for s in manifest["sources"]))
        pdf_entries = [s for s in manifest["sources"] if s["content_type"] == "application/pdf"]
        self.assertEqual(len(pdf_entries), 1)
        pdf_entry = pdf_entries[0]
        for key in (
            "artifact_id", "sha256", "content_type", "retrieved_at",
            "evidence_count", "extraction_status", "original_artifact",
        ):
            self.assertIn(key, pdf_entry)

    def test_rerunning_export_is_safe_and_replaces_stale_content(self):
        first = self._export()
        stray_file = first.output_dir / "sources" / "this-should-not-survive-a-rerun.txt"
        stray_file.write_text("stale")

        second = self._export()

        self.assertEqual(first.output_dir, second.output_dir)
        self.assertFalse(stray_file.exists(), "a re-export must fully replace the previous package, not merge into it")
        self.assertTrue((second.output_dir / "manifest.json").is_file())
        self.assertEqual(second.source_count, first.source_count)
        self.assertEqual(second.evidence_count, first.evidence_count)


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL integration tests")
class ExportErrorHandlingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)
        cls.storage_dir = tempfile.TemporaryDirectory()
        cls.export_root = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.storage_dir.cleanup()
        cls.export_root.cleanup()

    def test_nonexistent_run_id_raises_export_error(self):
        with self.assertRaises(ExportError):
            export_run(999_999_999, DATABASE_URL, Path(self.export_root.name))

    def test_missing_raw_artifact_is_recorded_not_fatal(self):
        # A fixture used only by this test, so it is never already-extracted by another
        # test's run of the same content (which would skip extraction and leave no evidence).
        fixture = Path(__file__).parent / "fixtures" / "export_missing_check.pdf"
        source_url = "file://" + fixture.resolve().as_posix()
        pipeline_result = run_pdf_pipeline(source_url, DATABASE_URL, Path(self.storage_dir.name))

        with psycopg.connect(DATABASE_URL) as conn:
            run_id = conn.execute(
                "SELECT id FROM app.acquisition_runs WHERE root_source_url = %s ORDER BY id DESC LIMIT 1",
                (source_url,),
            ).fetchone()[0]

        raw_path = Path(pipeline_result.artifact.raw_storage_ref)
        raw_bytes = raw_path.read_bytes()
        raw_path.unlink()
        try:
            result = export_run(run_id, DATABASE_URL, Path(self.export_root.name))
            source_dir = result.output_dir / "sources" / str(pipeline_result.artifact.id)
            source_json = json.loads((source_dir / "source.json").read_text(encoding="utf-8"))

            self.assertFalse(source_json["original_artifact"]["included"])
            self.assertIsNotNone(source_json["original_artifact"]["reason"])
            self.assertFalse((source_dir / "original.pdf").exists())
            # Evidence itself is entirely independent of the raw file's presence on disk.
            self.assertTrue((source_dir / "evidence.jsonl").read_text(encoding="utf-8").strip())
        finally:
            raw_path.write_bytes(raw_bytes)


if __name__ == "__main__":
    unittest.main()
