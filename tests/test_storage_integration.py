import os
import unittest

import psycopg

from kpi_crawler.migrations import migrate
from kpi_crawler.storage import AcquisitionRepository


DATABASE_URL = os.environ.get("DATABASE_URL")


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL integration tests")
class AcquisitionStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)

    def test_insert_duplicate_failure_lineage_and_lookup(self):
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.transaction():
                repository = AcquisitionRepository(conn)
                run = repository.create_run("https://example.test/root")
                discovery = repository.record_event(
                    run.id,
                    "discovery",
                    status="succeeded",
                    source_url="https://example.test/root",
                    source_type="web",
                )
                artifact = repository.store_artifact(
                    b"payload",
                    "s3://bucket/payload",
                    source_url="https://example.test/document",
                    source_type="web",
                    content_type="text/plain",
                )
                duplicate = repository.store_artifact(
                    b"payload",
                    "s3://bucket/other-location",
                    source_url="https://example.test/other",
                    content_type="application/octet-stream",
                )
                self.assertEqual(artifact.id, duplicate.id)
                self.assertEqual(artifact.raw_storage_ref, duplicate.raw_storage_ref)
                with self.assertRaises(psycopg.errors.RaiseException):
                    with conn.transaction():
                        conn.execute(
                            "UPDATE app.artifacts SET raw_storage_ref = %s WHERE id = %s",
                            ("s3://bucket/mutated", artifact.id),
                        )

                failed = repository.record_event(
                    run.id,
                    "acquisition",
                    status="failed",
                    parent_event_id=discovery.id,
                    source_url="https://example.test/document",
                    source_type="web",
                    error_message="upstream timeout",
                )
                completed = repository.complete_run(run.id, "failed", "upstream timeout")

                self.assertEqual(completed.status, "failed")
                self.assertEqual(failed.error_message, "upstream timeout")
                self.assertEqual(repository.find_artifact(artifact.sha256).id, artifact.id)
                lineage = repository.get_event_lineage(run.id)
                self.assertEqual([event.id for event in lineage], [discovery.id, failed.id])

    def test_event_type_is_constrained_to_the_controlled_set(self):
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.transaction():
                repository = AcquisitionRepository(conn)
                run = repository.create_run("https://example.test/constraint-check")
                with self.assertRaises(psycopg.errors.CheckViolation):
                    with conn.transaction():
                        repository.record_event(
                            run.id,
                            "not-a-real-event-type",
                            status="succeeded",
                            source_url="https://example.test/x",
                        )

    def test_complete_run_rejects_unknown_run_id(self):
        with psycopg.connect(DATABASE_URL) as conn:
            repository = AcquisitionRepository(conn)
            with self.assertRaises(LookupError):
                repository.complete_run(0, "succeeded")

    def test_get_artifact_rejects_unknown_artifact_id(self):
        with psycopg.connect(DATABASE_URL) as conn:
            repository = AcquisitionRepository(conn)
            with self.assertRaises(LookupError):
                repository.get_artifact(0)
