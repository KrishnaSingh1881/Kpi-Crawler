"""End-to-end retrieval-baseline pipeline test.

Uses a real PostgreSQL database and a real (small, synthetic) KPI dictionary,
but a stubbed embedding client (no Ollama/GPU dependency in tests) that
produces deterministic, structured vectors so retrieval ordering can be
asserted precisely.
"""

from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import openpyxl
import psycopg

from kpi_crawler.migrations import migrate
from kpi_crawler.retrieval.config import RetrievalSettings
from kpi_crawler.retrieval import pipeline as pipeline_module
from kpi_crawler.retrieval.report import write_report
from kpi_crawler.retrieval.store import prepare_connection
from kpi_crawler.storage import AcquisitionRepository

DATABASE_URL = os.environ.get("DATABASE_URL")
DIMENSION = 2560

KPI_HEADER = [
    "KPI_Code", "Domain", "Parameter", "Sub_Parameter", "Variable_Name", "Definition",
    "Unit", "Data_Type", "Formula", "Primary_Source", "Benchmark_Direction", "Priority_12M",
]


def _write_kpi_dictionary(path: Path, codes: list[str]) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "KPI_Dictionary"
    sheet.append(KPI_HEADER)
    for code in codes:
        sheet.append([code, "TEST", "Parameter", "Sub", f"{code} name", f"{code} definition",
                      "count", "integer", None, "source", "Higher", "P1"])
    workbook.save(path)


class _DeterministicEmbeddingClient:
    """Every text's vector is a one-hot at a position derived from its content hash,
    so cosine similarity is exactly 1.0 for identical content and orthogonal (0.0)
    otherwise — no live model or network call involved.
    """

    def __init__(self, config):
        self.config = config

    def embed_all(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.config.expected_dimension
        vector[hash(text) % self.config.expected_dimension] = 1.0
        return vector


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL integration tests")
class RetrievalPipelineIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)

    def _seed_evidence(self, conn, count: int) -> None:
        repository = AcquisitionRepository(conn)
        for i in range(count):
            artifact = repository.store_artifact(
                f"pipeline-test-content-{i}".encode(),
                f"/tmp/pipeline-test-{i}",
                source_type="test",
                content_type="application/pdf",
            )
            repository.create_evidence(
                artifact.id,
                f"claim {i}",
                f"value {i}",
                f"supporting context number {i}",
                source_url=f"https://example.test/pipeline/{i}",
                page_number=1,
                location=None,
                extractor="test-extractor",
                extractor_version="1",
            )
        conn.commit()

    def test_run_baseline_produces_complete_jsonl_and_reproducibility(self):
        with psycopg.connect(DATABASE_URL) as conn:
            self._seed_evidence(conn, 4)
            evidence_count = conn.execute("SELECT count(*) FROM app.evidence").fetchone()[0]

        with tempfile.TemporaryDirectory() as directory:
            kpi_path = Path(directory) / "kpis.xlsx"
            _write_kpi_dictionary(kpi_path, ["T01", "T02", "T03"])
            reports_dir = Path(directory) / "reports"

            settings = RetrievalSettings(
                database_url=DATABASE_URL,
                kpi_dictionary_path=kpi_path,
                embedding_model="test-pipeline-model",
                embedding_dimension=DIMENSION,
                top_k=2,
                reports_dir=reports_dir,
            )

            with patch.object(pipeline_module, "EmbeddingClient", _DeterministicEmbeddingClient):
                outcome = pipeline_module.run_baseline(settings)

            self.assertEqual(outcome.metrics.kpi_count, 3)
            self.assertEqual(outcome.metrics.evidence_count, evidence_count)
            self.assertTrue(outcome.run_dir.is_dir())

            for name, expected_rows in (
                ("evidence_to_kpi_A.jsonl", evidence_count * settings.top_k),
                ("evidence_to_kpi_B.jsonl", evidence_count * settings.top_k),
                ("kpi_to_evidence_A.jsonl", 3 * settings.top_k),
                ("kpi_to_evidence_B.jsonl", 3 * settings.top_k),
            ):
                path = outcome.run_dir / name
                self.assertTrue(path.is_file(), f"{name} was not written")
                lines = path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(lines), expected_rows, f"{name} row count mismatch")
                for line in lines:
                    record = json.loads(line)
                    self.assertIn("similarity", record)
                    self.assertIn("rank", record)
                    self.assertLessEqual(record["rank"], settings.top_k)

    def test_evidence_to_kpi_ranks_are_ordered_by_descending_similarity(self):
        with psycopg.connect(DATABASE_URL) as conn:
            self._seed_evidence(conn, 3)
            evidence_count = conn.execute("SELECT count(*) FROM app.evidence").fetchone()[0]

        with tempfile.TemporaryDirectory() as directory:
            kpi_path = Path(directory) / "kpis.xlsx"
            _write_kpi_dictionary(kpi_path, ["T01", "T02"])
            settings = RetrievalSettings(
                database_url=DATABASE_URL,
                kpi_dictionary_path=kpi_path,
                embedding_model="test-pipeline-model-2",
                embedding_dimension=DIMENSION,
                top_k=2,
                reports_dir=Path(directory) / "reports",
            )
            with patch.object(pipeline_module, "EmbeddingClient", _DeterministicEmbeddingClient):
                outcome = pipeline_module.run_baseline(settings)

            for name in ("evidence_to_kpi_A.jsonl", "kpi_to_evidence_A.jsonl"):
                lines = (outcome.run_dir / name).read_text(encoding="utf-8").splitlines()
                by_group: dict[tuple, list[dict]] = {}
                key_field = "evidence_id" if "evidence_to_kpi" in name else "kpi_code"
                for line in lines:
                    record = json.loads(line)
                    by_group.setdefault(record[key_field], []).append(record)
                for group in by_group.values():
                    similarities = [r["similarity"] for r in sorted(group, key=lambda r: r["rank"])]
                    self.assertEqual(similarities, sorted(similarities, reverse=True))

    def test_write_report_produces_markdown_with_key_sections(self):
        with psycopg.connect(DATABASE_URL) as conn:
            self._seed_evidence(conn, 3)

        with tempfile.TemporaryDirectory() as directory:
            kpi_path = Path(directory) / "kpis.xlsx"
            _write_kpi_dictionary(kpi_path, ["T01", "T02"])
            settings = RetrievalSettings(
                database_url=DATABASE_URL,
                kpi_dictionary_path=kpi_path,
                embedding_model="test-pipeline-model-report",
                embedding_dimension=DIMENSION,
                top_k=2,
                reports_dir=Path(directory) / "reports",
            )
            with patch.object(pipeline_module, "EmbeddingClient", _DeterministicEmbeddingClient):
                outcome = pipeline_module.run_baseline(settings)

            with psycopg.connect(DATABASE_URL) as report_conn:
                prepare_connection(report_conn)
                report_path = write_report(report_conn, outcome, settings)

            self.assertTrue(report_path.is_file())
            text = report_path.read_text(encoding="utf-8")
            for heading in ("Run metadata", "Corpus counts", "Timings", "Evidence sample", "KPI sample"):
                self.assertIn(heading, text)

    def test_missing_kpi_dictionary_fails_loudly(self):
        from kpi_crawler.errors import KPIDictionaryError

        with tempfile.TemporaryDirectory() as directory:
            reports_dir = Path(directory) / "reports"
            settings = RetrievalSettings(
                database_url=DATABASE_URL,
                kpi_dictionary_path=Path("/nonexistent/kpis.xlsx"),
                reports_dir=reports_dir,
            )
            with self.assertRaises(KPIDictionaryError):
                pipeline_module.run_baseline(settings)
            self.assertFalse(reports_dir.exists(), "a failed KPI load must not create a run directory")


if __name__ == "__main__":
    unittest.main()
