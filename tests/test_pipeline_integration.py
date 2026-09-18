import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import psycopg

from kpi_crawler.acquisition import AcquisitionError
from kpi_crawler.errors import DatabaseError, OperationalLimitError, UnsupportedContentError
from kpi_crawler.evidence_serialization import CANONICAL_SERIALIZER_VERSION, serialize_evidence
from kpi_crawler.migrations import migrate
from kpi_crawler.pipeline import ExtractionError, run_pdf_pipeline
from kpi_crawler.storage import AcquisitionRepository


DATABASE_URL = os.environ.get("DATABASE_URL")
FIXTURE = Path(__file__).parent / "fixtures" / "known.pdf"
SOURCE_URL = "file://" + FIXTURE.resolve().as_posix()
# A fixture used only by this one test, so re-processing it twice below is never
# affected by another test (in this file or another) having already extracted it.
REPEATABLE_FIXTURE = Path(__file__).parent / "fixtures" / "repeatable.pdf"


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL integration tests")
class PipelineIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)
        cls.storage_dir = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.storage_dir.cleanup()

    def test_source_to_artifact_extraction_evidence_database_and_provenance(self):
        result = run_pdf_pipeline(SOURCE_URL, DATABASE_URL, Path(self.storage_dir.name))

        self.assertTrue(Path(result.artifact.raw_storage_ref).exists())
        self.assertEqual(result.artifact.source_url, SOURCE_URL)
        self.assertTrue(result.evidence)
        evidence = result.evidence[0]
        self.assertEqual(evidence.evidence.artifact_id, result.artifact.id)
        self.assertEqual(evidence.source_url, SOURCE_URL)
        self.assertEqual(evidence.page_number, 1)
        self.assertIn("KPI", evidence.evidence.claim)
        self.assertIn("42", evidence.evidence.value)
        self.assertIn("KPI: 42", evidence.evidence.supporting_context)

        with psycopg.connect(DATABASE_URL) as conn:
            retrieved = AcquisitionRepository(conn).get_evidence(evidence.evidence.id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.artifact.sha256, result.artifact.sha256)
        self.assertEqual(retrieved.evidence.id, evidence.evidence.id)

        expected_canonical_text = serialize_evidence(
            retrieved.evidence.evidence_type,
            retrieved.evidence.claim,
            retrieved.evidence.value,
            retrieved.evidence.supporting_context,
            retrieved.evidence.structure,
        )
        self.assertEqual(retrieved.evidence.canonical_text, expected_canonical_text)
        self.assertEqual(retrieved.evidence.canonical_serializer_version, CANONICAL_SERIALIZER_VERSION)
        self.assertEqual(retrieved.evidence.supporting_context, evidence.evidence.supporting_context)

    def test_extraction_failure_is_recorded(self):
        with patch("kpi_crawler.pipeline.PdfExtractor.extract", side_effect=RuntimeError("bad PDF")):
            with self.assertRaises(ExtractionError):
                run_pdf_pipeline(SOURCE_URL, DATABASE_URL, Path(self.storage_dir.name))

        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute(
                """
                SELECT status, error_message
                FROM app.acquisition_events
                WHERE source_url = %s AND event_type = 'extraction'
                ORDER BY id DESC LIMIT 1
                """,
                (SOURCE_URL,),
            ).fetchone()
        self.assertEqual(row, ("failed", "bad PDF"))

    def test_acquisition_failure_is_recorded(self):
        missing_source = "file:///tmp/kpi-crawler-missing-fixture.pdf"
        with self.assertRaises(AcquisitionError):
            run_pdf_pipeline(missing_source, DATABASE_URL, Path(self.storage_dir.name))

        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute(
                """
                SELECT status, error_message
                FROM app.acquisition_events
                WHERE source_url = %s AND event_type = 'acquisition'
                ORDER BY id DESC LIMIT 1
                """,
                (missing_source,),
            ).fetchone()
        self.assertEqual(row[0], "failed")
        self.assertIn("failed to read", row[1])

    def test_structured_pdf_evidence_and_extraction_metadata_are_retrievable(self):
        fixture = Path(__file__).parent / "fixtures" / "structured.pdf"
        result = run_pdf_pipeline(fixture.resolve().as_uri(), DATABASE_URL, Path(self.storage_dir.name))
        table = next(item for item in result.evidence if item.evidence.evidence_type == "table")
        visual = next(item for item in result.evidence if item.evidence.evidence_type == "visual")

        self.assertEqual(table.evidence.structure["headers"], ["Metric", "Value"])
        self.assertEqual(table.evidence.structure["rows"][0], ["Revenue", "120"])
        self.assertEqual(visual.evidence.structure["kind"], "bitmap")
        self.assertIsNotNone(table.evidence.extraction_id)

        with psycopg.connect(DATABASE_URL) as conn:
            repository = AcquisitionRepository(conn)
            extraction = repository.get_extraction(table.evidence.extraction_id)
            retrieved = repository.get_evidence(table.evidence.id)
        self.assertEqual(extraction.status, "succeeded")
        self.assertEqual(extraction.metadata["table_count"], 1)
        self.assertEqual(retrieved.evidence.structure, table.evidence.structure)

        expected_table_canonical_text = serialize_evidence(
            table.evidence.evidence_type,
            table.evidence.claim,
            table.evidence.value,
            table.evidence.supporting_context,
            table.evidence.structure,
        )
        self.assertEqual(table.evidence.canonical_text, expected_table_canonical_text)
        self.assertEqual(table.evidence.canonical_serializer_version, CANONICAL_SERIALIZER_VERSION)

    def test_unsupported_content_type_is_rejected_but_artifact_is_preserved(self):
        with tempfile.TemporaryDirectory() as source_dir:
            text_file = Path(source_dir) / "notes.txt"
            text_file.write_text("not a PDF")
            source_url = text_file.resolve().as_uri()

            with self.assertRaises(UnsupportedContentError):
                run_pdf_pipeline(source_url, DATABASE_URL, Path(self.storage_dir.name))

            with psycopg.connect(DATABASE_URL) as conn:
                repository = AcquisitionRepository(conn)
                sha256 = hashlib.sha256(text_file.read_bytes()).hexdigest()
                artifact = repository.find_artifact(sha256)
                event = conn.execute(
                    """
                    SELECT status, error_message
                    FROM app.acquisition_events
                    WHERE source_url = %s AND event_type = 'extraction'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (source_url,),
                ).fetchone()
                run_status = conn.execute(
                    "SELECT status FROM app.acquisition_runs WHERE root_source_url = %s ORDER BY id DESC LIMIT 1",
                    (source_url,),
                ).fetchone()

        self.assertIsNotNone(artifact, "the fetched content must still be persisted as an artifact")
        self.assertEqual(event[0], "failed")
        self.assertIn("unsupported content type", event[1])
        self.assertEqual(run_status[0], "failed")

    def test_operational_limit_exceeded_is_rejected_and_recorded(self):
        with self.assertRaises(OperationalLimitError):
            run_pdf_pipeline(SOURCE_URL, DATABASE_URL, Path(self.storage_dir.name), max_artifact_bytes=100)

        with psycopg.connect(DATABASE_URL) as conn:
            event = conn.execute(
                """
                SELECT status, error_message
                FROM app.acquisition_events
                WHERE source_url = %s AND event_type = 'acquisition'
                ORDER BY id DESC LIMIT 1
                """,
                (SOURCE_URL,),
            ).fetchone()
        self.assertEqual(event[0], "failed")
        self.assertIn("exceeds the 100-byte limit", event[1])

    def test_evidence_storage_failure_preserves_already_acquired_artifact(self):
        with patch.object(
            AcquisitionRepository,
            "create_evidence",
            side_effect=psycopg.errors.OperationalError("simulated evidence storage failure"),
        ):
            with self.assertRaises(DatabaseError):
                run_pdf_pipeline(SOURCE_URL, DATABASE_URL, Path(self.storage_dir.name))

        with psycopg.connect(DATABASE_URL) as conn:
            repository = AcquisitionRepository(conn)
            sha256 = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
            artifact = repository.find_artifact(sha256)
            event = conn.execute(
                """
                SELECT status, error_message
                FROM app.acquisition_events
                WHERE source_url = %s AND event_type = 'extraction'
                ORDER BY id DESC LIMIT 1
                """,
                (SOURCE_URL,),
            ).fetchone()

        self.assertIsNotNone(artifact, "an already-persisted artifact must survive a later storage failure")
        self.assertEqual(event[0], "failed")
        self.assertIn("failed to persist evidence", event[1])

    def test_reprocessing_identical_content_does_not_duplicate_evidence(self):
        source_url = REPEATABLE_FIXTURE.resolve().as_uri()

        first = run_pdf_pipeline(source_url, DATABASE_URL, Path(self.storage_dir.name))
        second = run_pdf_pipeline(source_url, DATABASE_URL, Path(self.storage_dir.name))

        self.assertTrue(first.evidence, "the first processing of new content must produce evidence")
        self.assertEqual(first.artifact.id, second.artifact.id)
        self.assertEqual(second.evidence, (), "reprocessing already-extracted content must not create more evidence")

        with psycopg.connect(DATABASE_URL) as conn:
            evidence_count = conn.execute(
                "SELECT count(*) FROM app.evidence WHERE artifact_id = %s", (first.artifact.id,)
            ).fetchone()[0]
            extraction_count = conn.execute(
                "SELECT count(*) FROM app.extractions WHERE artifact_id = %s AND status = 'succeeded'",
                (first.artifact.id,),
            ).fetchone()[0]
        self.assertEqual(evidence_count, len(first.evidence))
        self.assertEqual(extraction_count, 1)
