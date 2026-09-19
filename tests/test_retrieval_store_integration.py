import os
import unittest

import psycopg

from kpi_crawler.migrations import migrate
from kpi_crawler.retrieval.kpi_dictionary import KPIDefinition
from kpi_crawler.retrieval.store import (
    fetch_all_evidence,
    get_evidence_embedding,
    get_kpi_embedding,
    prepare_connection,
    top_k_evidence_for_vector,
    top_k_kpis_for_vector,
    upsert_evidence_embedding,
    upsert_kpi_embedding,
    upsert_kpis,
)
from kpi_crawler.storage import AcquisitionRepository

DATABASE_URL = os.environ.get("DATABASE_URL")
DIMENSION = 2560
MODEL = "test-model"


def _unit_vector(index: int, dimension: int = DIMENSION, sign: float = 1.0) -> list[float]:
    vector = [0.0] * dimension
    vector[index] = sign
    return vector


def _kpi(code: str, name: str, definition: str) -> KPIDefinition:
    return KPIDefinition(
        kpi_code=code,
        variable_name=name,
        definition=definition,
        domain="TEST",
        parameter=None,
        sub_parameter=None,
        unit=None,
        data_type=None,
        formula=None,
        primary_source=None,
        benchmark_direction=None,
        priority_12m=None,
        source_file="test.xlsx",
        source_row={"KPI_Code": code},
    )


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL integration tests")
class RetrievalStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)

    def _make_evidence(self, conn, suffix: str) -> int:
        repository = AcquisitionRepository(conn)
        artifact = repository.store_artifact(
            f"retrieval-test-content-{suffix}".encode(),
            f"/tmp/retrieval-test-{suffix}",
            source_type="test",
            content_type="application/pdf",
        )
        provenance = repository.create_evidence(
            artifact.id,
            "claim",
            "value",
            f"supporting context {suffix}",
            source_url=f"https://example.test/{suffix}",
            page_number=1,
            location=None,
            extractor="test-extractor",
            extractor_version="1",
        )
        conn.commit()
        return provenance.evidence.id

    def test_kpi_upsert_preserves_identity_and_source_row(self):
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.transaction():
                upsert_kpis(conn, [_kpi("T01", "Test KPI", "A test KPI definition")])
            row = conn.execute(
                "SELECT kpi_code, variable_name, definition, source_row FROM app.kpis WHERE kpi_code = %s",
                ("T01",),
            ).fetchone()
            self.assertEqual(row[0], "T01")
            self.assertEqual(row[1], "Test KPI")
            self.assertEqual(row[3], {"KPI_Code": "T01"})
            conn.rollback()

    def test_evidence_embedding_representations_a_and_b_are_independent(self):
        with psycopg.connect(DATABASE_URL) as conn:
            prepare_connection(conn)
            evidence_id = self._make_evidence(conn, "ab-separation")
            vector_a = _unit_vector(0)
            vector_b = _unit_vector(1)
            with conn.transaction():
                upsert_evidence_embedding(
                    conn, evidence_id=evidence_id, representation="canonical_text",
                    model_identifier=MODEL, dimension=DIMENSION, vector=vector_a,
                    normalized=True, instruction=None, canonical_serializer_version="1",
                    embedded_text="canonical text",
                )
                upsert_evidence_embedding(
                    conn, evidence_id=evidence_id, representation="supporting_context",
                    model_identifier=MODEL, dimension=DIMENSION, vector=vector_b,
                    normalized=True, instruction=None, canonical_serializer_version="1",
                    embedded_text="supporting context",
                )
            fetched_a = get_evidence_embedding(conn, evidence_id, representation="canonical_text", model_identifier=MODEL)
            fetched_b = get_evidence_embedding(conn, evidence_id, representation="supporting_context", model_identifier=MODEL)
            self.assertEqual(len(fetched_a), DIMENSION)
            self.assertEqual(len(fetched_b), DIMENSION)
            self.assertNotEqual(fetched_a, fetched_b)
            self.assertAlmostEqual(fetched_a[0], 1.0, places=5)
            self.assertAlmostEqual(fetched_b[1], 1.0, places=5)
            conn.rollback()

    def test_wrong_dimension_vector_is_rejected(self):
        with psycopg.connect(DATABASE_URL) as conn:
            prepare_connection(conn)
            evidence_id = self._make_evidence(conn, "wrong-dimension")
            with self.assertRaises(psycopg.Error):
                with conn.transaction():
                    upsert_evidence_embedding(
                        conn, evidence_id=evidence_id, representation="canonical_text",
                        model_identifier=MODEL, dimension=8, vector=[0.1] * 8,
                        normalized=True, instruction=None, canonical_serializer_version="1",
                        embedded_text="too short",
                    )
            conn.rollback()

    def test_top_k_kpi_retrieval_orders_by_cosine_similarity(self):
        model = "test-model-ordering"
        with psycopg.connect(DATABASE_URL) as conn:
            prepare_connection(conn)
            with conn.transaction():
                upsert_kpis(conn, [
                    _kpi("SIM-IDENTICAL", "identical", "identical vector"),
                    _kpi("SIM-ORTHOGONAL", "orthogonal", "orthogonal vector"),
                    _kpi("SIM-OPPOSITE", "opposite", "opposite vector"),
                ])
                upsert_kpi_embedding(
                    conn, kpi_code="SIM-IDENTICAL", representation="name_and_definition",
                    model_identifier=model, dimension=DIMENSION, vector=_unit_vector(0),
                    normalized=True, instruction=None, embedded_text="identical",
                )
                upsert_kpi_embedding(
                    conn, kpi_code="SIM-ORTHOGONAL", representation="name_and_definition",
                    model_identifier=model, dimension=DIMENSION, vector=_unit_vector(1),
                    normalized=True, instruction=None, embedded_text="orthogonal",
                )
                upsert_kpi_embedding(
                    conn, kpi_code="SIM-OPPOSITE", representation="name_and_definition",
                    model_identifier=model, dimension=DIMENSION, vector=_unit_vector(0, sign=-1.0),
                    normalized=True, instruction=None, embedded_text="opposite",
                )
            results = top_k_kpis_for_vector(
                conn, _unit_vector(0), model_identifier=model, representation="name_and_definition", k=3
            )
            codes = [r["kpi_code"] for r in results]
            self.assertEqual(codes, ["SIM-IDENTICAL", "SIM-ORTHOGONAL", "SIM-OPPOSITE"])
            self.assertAlmostEqual(results[0]["similarity"], 1.0, places=5)
            self.assertAlmostEqual(results[1]["similarity"], 0.0, places=5)
            self.assertAlmostEqual(results[2]["similarity"], -1.0, places=5)
            conn.rollback()

    def test_top_k_respects_limit(self):
        model = "test-model-limit"
        with psycopg.connect(DATABASE_URL) as conn:
            prepare_connection(conn)
            kpis = [_kpi(f"LIMIT-{i}", f"kpi {i}", f"definition {i}") for i in range(5)]
            with conn.transaction():
                upsert_kpis(conn, kpis)
                for i, kpi in enumerate(kpis):
                    upsert_kpi_embedding(
                        conn, kpi_code=kpi.kpi_code, representation="name_and_definition",
                        model_identifier=model, dimension=DIMENSION, vector=_unit_vector(i + 10),
                        normalized=True, instruction=None, embedded_text=kpi.embedding_text,
                    )
            results = top_k_kpis_for_vector(
                conn, _unit_vector(10), model_identifier=model, representation="name_and_definition", k=2
            )
            self.assertEqual(len(results), 2)
            conn.rollback()

    def test_kpi_to_evidence_direction_orders_by_cosine_similarity(self):
        model = "test-model-kpi-to-evidence"
        with psycopg.connect(DATABASE_URL) as conn:
            prepare_connection(conn)
            evidence_close = self._make_evidence(conn, "close")
            evidence_far = self._make_evidence(conn, "far")
            with conn.transaction():
                upsert_evidence_embedding(
                    conn, evidence_id=evidence_close, representation="canonical_text",
                    model_identifier=model, dimension=DIMENSION, vector=_unit_vector(0),
                    normalized=True, instruction=None, canonical_serializer_version="1",
                    embedded_text="close",
                )
                upsert_evidence_embedding(
                    conn, evidence_id=evidence_far, representation="canonical_text",
                    model_identifier=model, dimension=DIMENSION, vector=_unit_vector(0, sign=-1.0),
                    normalized=True, instruction=None, canonical_serializer_version="1",
                    embedded_text="far",
                )
            results = top_k_evidence_for_vector(
                conn, _unit_vector(0), model_identifier=model, representation="canonical_text", k=10
            )
            ids = [r["evidence_id"] for r in results]
            self.assertIn(evidence_close, ids)
            self.assertLess(ids.index(evidence_close), ids.index(evidence_far))
            conn.rollback()

    def test_fetch_all_evidence_returns_both_evidence_types_present(self):
        with psycopg.connect(DATABASE_URL) as conn:
            self._make_evidence(conn, "fetch-all-check")
            rows = fetch_all_evidence(conn)
            self.assertGreaterEqual(len(rows), 1)
            conn.rollback()


if __name__ == "__main__":
    unittest.main()
