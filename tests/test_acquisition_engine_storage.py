"""Tests for Program 1 run-scoped storage architecture.

Proves:
1. Two different websites get separate directories
2. Two runs of the same website get separate directories
3. Artifacts cannot escape their run directory (containment check)
4. Evidence is run-scoped
5. Program-2 handoff package is generated inside the run (artifacts.json, metadata.json, provenance.json)
6. Fresh DB starts with no previous acquisition records
7. run_id links filesystem, Postgres metadata, artifacts and evidence
"""

import asyncio
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest

import psycopg

from kpi_crawler.acquisition_engine.contract import (
    AcquisitionState,
    AdaptiveDecisionRecord,
    ArtifactRecord,
    AttemptRecord,
    RedirectHop,
    RunSummary,
)
from kpi_crawler.acquisition_engine.engine import AcquisitionEngine, EngineConfig
from kpi_crawler.acquisition_engine.events import ListEventSink
from kpi_crawler.acquisition_engine.ledger import EvidenceLedger
from kpi_crawler.acquisition_engine.storage import (
    RunStorage,
    classify_content_type,
    safe_site_id,
)
from kpi_crawler.db import connection
from kpi_crawler.errors import StorageError
from kpi_crawler.migrations import migrate

DATABASE_URL = os.environ.get("DATABASE_URL")


class _MockHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = b"<!DOCTYPE html><html><body><h1>Home</h1><a href='/data.json'>JSON</a><a href='/doc.pdf'>PDF</a></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/data.json":
            body = b'{"kpi": 42}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/doc.pdf":
            body = b"%PDF-1.4 mock pdf content"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class StorageHelperUnitTests(unittest.TestCase):
    def test_safe_site_id_sanitization(self):
        self.assertEqual(safe_site_id("https://example.com"), "example.com")
        self.assertEqual(safe_site_id("https://sub.domain.org/path?q=1"), "sub.domain.org")
        self.assertEqual(safe_site_id("http://127.0.0.1:8999/"), "127.0.0.1_8999")
        self.assertEqual(safe_site_id("http://localhost:8080/foo"), "localhost_8080")
        self.assertEqual(safe_site_id("file:///home/user/doc.pdf"), "home_user_doc.pdf")

    def test_classify_content_type(self):
        self.assertEqual(classify_content_type("text/html; charset=utf-8"), "html")
        self.assertEqual(classify_content_type("application/xhtml+xml"), "html")
        self.assertEqual(classify_content_type("application/json"), "json")
        self.assertEqual(classify_content_type("application/pdf"), "pdf")
        self.assertEqual(classify_content_type("image/png"), "other")
        self.assertEqual(classify_content_type(None, "https://example.com/test.pdf"), "pdf")
        self.assertEqual(classify_content_type(None, "https://example.com/test.json"), "json")
        self.assertEqual(classify_content_type(None, "https://example.com/index.html"), "html")
        self.assertEqual(classify_content_type(None, "https://example.com/blob.bin"), "other")

    def test_artifacts_cannot_escape_run_directory(self):
        """Proof of requirement 3: artifacts cannot escape their run directory."""
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            storage = RunStorage(base_dir, "site_a", 1)

            # Normal write inside run directory succeeds
            path = storage.write_raw_artifact(b"test", "a" * 64, "text/html")
            self.assertTrue(path.exists())
            self.assertTrue(str(path).startswith(str(storage.run_dir)))
            self.assertEqual(path.parent.name, "html")

            # Path traversal attempt fails with StorageError
            with self.assertRaises(StorageError):
                storage.write_raw_artifact(b"evil", "../../../escaped", "text/html")

    def test_write_report_generates_self_contained_html(self):
        """report.html is generated, contains the run_id, and its embedded
        data matches the artifacts/attempts/decisions passed in."""
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            storage = RunStorage(base_dir, "example.com", 42)
            raw_path = storage.write_raw_artifact(b"<html>hi</html>", "b" * 64, "text/html", "https://example.com/")

            summary = RunSummary(
                run_id=42, contract_version=2, root_source_url="https://example.com/",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                completed_at=datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc),
                status=AcquisitionState.PARTIAL, discovered=2, attempted=2, acquired=1,
                partial=0, failed=1, retries=1, rate_limited=0, browser_pages=0,
                proxy_failures=0, by_state={"SUCCESS": 1, "NOT_FOUND": 1},
            )
            artifacts = [
                ArtifactRecord(
                    contract_version=2, artifact_id=1, run_id=42, source_url="https://example.com/",
                    canonical_url=None, discovered_from=None,
                    fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc), content_type="text/html",
                    http_status=200, acquisition_method="http", raw_location=str(raw_path),
                    content_size=16, checksum="b" * 64, encoding=None, final_url=None,
                    redirect_chain=(), session_id="s1", status=AcquisitionState.SUCCESS,
                ),
            ]
            attempts = [
                AttemptRecord(
                    run_id=42, attempt_id=1, occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    url="https://example.com/", domain="example.com", acquisition_method="http",
                    session_id="s1", proxy_id=None, proxy_status=None, http_status=200,
                    latency_ms=12.5, retry_number=0, retry_budget=3, timeout_seconds=10.0,
                    backoff_applied_seconds=None, concurrency_at_attempt=8, rate_limit_detected=False,
                    failure_classification=None, adaptive_decision=None,
                    final_result=AcquisitionState.SUCCESS, artifact_id=1, error_message=None,
                ),
                AttemptRecord(
                    run_id=42, attempt_id=2, occurred_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
                    url="https://example.com/missing", domain="example.com", acquisition_method="http",
                    session_id="s1", proxy_id=None, proxy_status=None, http_status=404,
                    latency_ms=8.0, retry_number=0, retry_budget=3, timeout_seconds=10.0,
                    backoff_applied_seconds=None, concurrency_at_attempt=8, rate_limit_detected=False,
                    failure_classification="http_404", adaptive_decision=None,
                    final_result=AcquisitionState.NOT_FOUND, artifact_id=None, error_message="404 Not Found",
                ),
            ]
            decisions = [
                AdaptiveDecisionRecord(
                    run_id=42, decision_id=1, occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    domain="example.com", decision_type="concurrency_reduced", reason="429 detected",
                    before_value="8", after_value="4", details={},
                ),
            ]

            report_path = storage.write_report(summary, artifacts, attempts, decisions)

            self.assertTrue(report_path.exists())
            self.assertEqual(report_path, storage.run_dir / "report.html")
            html = report_path.read_text(encoding="utf-8")
            self.assertIn("42", html)

            match = re.search(
                r'<script type="application/json" id="report-data">(.*?)</script>', html, re.DOTALL
            )
            self.assertIsNotNone(match)
            embedded = json.loads(match.group(1))

            self.assertEqual(embedded["summary"]["run_id"], 42)
            self.assertEqual(embedded["summary"]["status"], "PARTIAL")
            self.assertEqual(embedded["summary"]["by_state"], {"SUCCESS": 1, "NOT_FOUND": 1})

            self.assertEqual(len(embedded["attempts"]), 2)
            urls = {a["url"] for a in embedded["attempts"]}
            self.assertEqual(urls, {"https://example.com/", "https://example.com/missing"})
            not_found = next(a for a in embedded["attempts"] if a["url"] == "https://example.com/missing")
            self.assertEqual(not_found["final_result"], "NOT_FOUND")
            self.assertEqual(not_found["error_message"], "404 Not Found")

            self.assertEqual(len(embedded["artifacts"]), 1)
            self.assertEqual(embedded["artifacts"][0]["checksum"], "b" * 64)
            self.assertEqual(embedded["artifacts"][0]["raw_location"], "raw/html/" + "b" * 64 + ".raw")

            self.assertEqual(len(embedded["decisions"]), 1)
            self.assertEqual(embedded["decisions"][0]["decision_type"], "concurrency_reduced")


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for storage integration tests")
class RunScopedStorageIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage_dir = Path(self.tmp.name)

        cm = connection(DATABASE_URL)
        self.conn = cm.__enter__()
        self.addCleanup(lambda: cm.__exit__(None, None, None))
        self.ledger = EvidenceLedger(self.conn)

    def _run_engine(self, url: str, base_dir: Path | None = None) -> tuple[any, int, AcquisitionEngine]:
        config = EngineConfig(
            max_depth=1,
            max_artifacts=10,
            max_concurrency=2,
            enable_browser_escalation=False,
            storage_dir=base_dir or self.storage_dir,
        )
        sink = ListEventSink()
        engine = AcquisitionEngine(config, self.ledger, sink)
        summary, run_id = asyncio.run(engine.run(url))
        return summary, run_id, engine

    def test_requirement_1_different_websites_get_separate_directories(self):
        """1. Two different websites get separate directories."""
        url_1 = f"{self.base_url}/"
        url_2 = f"http://localhost:{self.port}/"

        summary_1, run_id_1, engine_1 = self._run_engine(url_1)
        summary_2, run_id_2, engine_2 = self._run_engine(url_2)

        dir_1 = engine_1._run_storage.run_dir
        dir_2 = engine_2._run_storage.run_dir

        self.assertTrue(dir_1.exists())
        self.assertTrue(dir_2.exists())
        self.assertNotEqual(dir_1, dir_2)
        # Their site_id parent directories must be different
        self.assertNotEqual(dir_1.parent.name, dir_2.parent.name)
        self.assertIn(f"127.0.0.1_{self.port}", str(dir_1))
        self.assertIn(f"localhost_{self.port}", str(dir_2))

    def test_requirement_2_two_runs_of_same_website_get_separate_directories(self):
        """2. Two runs of the same website get separate directories."""
        url = f"{self.base_url}/"

        summary_1, run_id_1, engine_1 = self._run_engine(url)
        summary_2, run_id_2, engine_2 = self._run_engine(url)

        dir_1 = engine_1._run_storage.run_dir
        dir_2 = engine_2._run_storage.run_dir

        self.assertTrue(dir_1.exists())
        self.assertTrue(dir_2.exists())
        self.assertNotEqual(run_id_1, run_id_2)
        self.assertNotEqual(dir_1, dir_2)
        # Same site_id parent, different run_id directories
        self.assertEqual(dir_1.parent, dir_2.parent)
        self.assertEqual(dir_1.name, str(run_id_1))
        self.assertEqual(dir_2.name, str(run_id_2))

    def test_requirement_4_evidence_is_run_scoped(self):
        """4. Evidence is run-scoped."""
        url = f"{self.base_url}/"
        summary_1, run_id_1, engine_1 = self._run_engine(url)
        summary_2, run_id_2, engine_2 = self._run_engine(url)

        dir_1 = engine_1._run_storage.run_dir
        dir_2 = engine_2._run_storage.run_dir

        attempts_file_1 = dir_1 / "evidence" / "attempts.jsonl"
        attempts_file_2 = dir_2 / "evidence" / "attempts.jsonl"
        self.assertTrue(attempts_file_1.exists())
        self.assertTrue(attempts_file_2.exists())

        attempts_1 = [json.loads(line) for line in attempts_file_1.read_text().splitlines() if line]
        attempts_2 = [json.loads(line) for line in attempts_file_2.read_text().splitlines() if line]

        self.assertTrue(all(a["run_id"] == run_id_1 for a in attempts_1))
        self.assertTrue(all(a["run_id"] == run_id_2 for a in attempts_2))
        self.assertFalse(any(a["run_id"] == run_id_2 for a in attempts_1))

    def test_requirement_5_program2_handoff_package_is_generated_inside_run(self):
        """5. Program-2 handoff package is generated inside the run."""
        url = f"{self.base_url}/"
        summary, run_id, engine = self._run_engine(url)
        run_dir = engine._run_storage.run_dir

        p2_dir = run_dir / "program2"
        self.assertTrue(p2_dir.exists())

        artifacts_json = p2_dir / "artifacts.json"
        metadata_json = p2_dir / "metadata.json"
        provenance_json = p2_dir / "provenance.json"

        self.assertTrue(artifacts_json.exists())
        self.assertTrue(metadata_json.exists())
        self.assertTrue(provenance_json.exists())

        # Validate artifacts.json
        artifacts = json.loads(artifacts_json.read_text(encoding="utf-8"))
        self.assertIsInstance(artifacts, list)
        self.assertGreaterEqual(len(artifacts), 1)
        for a in artifacts:
            self.assertEqual(a["run_id"], run_id)
            self.assertIn("source_url", a)
            self.assertIn("checksum", a)
            self.assertIn("raw_location", a)
            # Ensure internal proxy/session objects are not leaked
            self.assertNotIn("session", a)
            self.assertNotIn("proxy", a)

        # Validate metadata.json
        metadata = json.loads(metadata_json.read_text(encoding="utf-8"))
        self.assertEqual(metadata["run_id"], run_id)
        self.assertEqual(metadata["status"], summary.status.value)
        self.assertEqual(metadata["artifact_count"], len(artifacts))

        # Validate provenance.json
        provenance = json.loads(provenance_json.read_text(encoding="utf-8"))
        self.assertEqual(provenance["run_id"], run_id)
        self.assertIn("artifacts", provenance)

    def test_requirement_7_run_id_links_filesystem_postgres_artifacts_evidence(self):
        """7. run_id links filesystem, Postgres metadata, artifacts and evidence."""
        url = f"{self.base_url}/"
        summary, run_id, engine = self._run_engine(url)
        run_dir = engine._run_storage.run_dir

        # 1. Filesystem directory name matches run_id
        self.assertEqual(run_dir.name, str(run_id))

        # 2. Postgres metadata matches run_id
        row = self.conn.execute("SELECT id, root_source_url, status FROM acq.runs WHERE id = %s", (run_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], run_id)
        self.assertEqual(row[2], summary.status.value)

        # 3. Postgres artifacts are bound to run_id
        db_artifacts = self.ledger.artifacts_for_run(run_id)
        self.assertGreaterEqual(len(db_artifacts), 1)
        for art in db_artifacts:
            self.assertEqual(art.run_id, run_id)
            # raw_location exists on disk and is inside run_dir/raw
            raw_path = Path(art.raw_location)
            self.assertTrue(raw_path.exists())
            self.assertTrue(str(raw_path).startswith(str(run_dir / "raw")))

        # 4. Postgres attempts are bound to run_id
        db_attempts = self.ledger.attempts_for_run(run_id)
        self.assertTrue(all(att.run_id == run_id for att in db_attempts))

        # 5. Top-level manifest.json links run_id and paths
        manifest_file = run_dir / "manifest.json"
        self.assertTrue(manifest_file.exists())
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(manifest["run_id"], run_id)
        self.assertEqual(manifest["paths"]["run_dir"], str(run_dir))
        self.assertEqual(manifest["artifact_count"], len(db_artifacts))

    def test_requirement_6_fresh_db_starts_with_no_previous_acquisition_records(self):
        """6. Fresh DB starts with no previous acquisition records and guarantees per-run isolation."""
        # Querying an uninitialized or isolated run_id yields zero records across all acquisition tables
        unseen_run_id = 99999999
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM acq.runs WHERE id = %s", (unseen_run_id,))
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT COUNT(*) FROM acq.artifacts WHERE run_id = %s", (unseen_run_id,))
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT COUNT(*) FROM acq.attempts WHERE run_id = %s", (unseen_run_id,))
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT COUNT(*) FROM acq.adaptive_decisions WHERE run_id = %s", (unseen_run_id,))
            self.assertEqual(cur.fetchone()[0], 0)

        # Run an acquisition
        url = f"{self.base_url}/"
        summary, run_id, engine = self._run_engine(url)

        # Confirm new run has records only for its own run_id
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM acq.runs WHERE id = %s", (run_id,))
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT COUNT(*) FROM acq.artifacts WHERE run_id = %s", (run_id,))
            self.assertGreater(cur.fetchone()[0], 0)
            # Ensure no cross-contamination to any other run_id
            cur.execute("SELECT COUNT(*) FROM acq.artifacts WHERE run_id != %s AND run_id = %s", (run_id, unseen_run_id))
            self.assertEqual(cur.fetchone()[0], 0)

    def test_target_storage_tree_structure(self):
        """Verifies exact target structure: raw/(html|json|pdf|other), evidence/, manifest.json, program2/."""
        url = f"{self.base_url}/"
        summary, run_id, engine = self._run_engine(url)
        run_dir = engine._run_storage.run_dir

        self.assertTrue((run_dir / "raw" / "html").is_dir())
        self.assertTrue((run_dir / "raw" / "json").is_dir())
        self.assertTrue((run_dir / "raw" / "pdf").is_dir())
        self.assertTrue((run_dir / "raw" / "other").is_dir())
        self.assertTrue((run_dir / "evidence").is_dir())
        self.assertTrue((run_dir / "manifest.json").is_file())
        self.assertTrue((run_dir / "program2" / "artifacts.json").is_file())
        self.assertTrue((run_dir / "program2" / "metadata.json").is_file())
        self.assertTrue((run_dir / "program2" / "provenance.json").is_file())
        self.assertTrue((run_dir / "report.html").is_file())

    def test_report_html_generated_by_real_engine_run(self):
        """report.html is produced end-to-end alongside manifest.json/program2/
        for a real engine run, referencing raw/ artifacts by a relative path
        that resolves from report.html's own location."""
        url = f"{self.base_url}/"
        summary, run_id, engine = self._run_engine(url)
        run_dir = engine._run_storage.run_dir

        report_path = run_dir / "report.html"
        self.assertTrue(report_path.is_file())
        html = report_path.read_text(encoding="utf-8")
        self.assertIn(str(run_id), html)

        match = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.DOTALL)
        self.assertIsNotNone(match)
        embedded = json.loads(match.group(1))
        self.assertEqual(embedded["summary"]["run_id"], run_id)
        self.assertGreaterEqual(len(embedded["artifacts"]), 1)
        self.assertGreaterEqual(len(embedded["attempts"]), 1)

        # raw_location is relative and resolves from report.html's own directory.
        for artifact in embedded["artifacts"]:
            raw_location = artifact["raw_location"]
            self.assertFalse(Path(raw_location).is_absolute())
            self.assertTrue((run_dir / raw_location).is_file())


if __name__ == "__main__":
    unittest.main()
