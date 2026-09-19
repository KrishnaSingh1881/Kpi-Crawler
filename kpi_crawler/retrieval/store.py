"""Persistence for the KPI dictionary, evidence/KPI embeddings, and top-K retrieval.

`app.evidence` is read-only from this module's point of view: it is never
written to, altered, filtered, merged, or split here. Embeddings live in their
own tables, keyed back to `evidence_id`/`kpi_code`.
"""

from dataclasses import dataclass
from typing import Any

from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg import Connection
from psycopg.types.json import Jsonb

from .embedding_client import checksum as text_checksum
from .kpi_dictionary import KPIDefinition


def prepare_connection(conn: Connection[Any]) -> None:
    """Register the pgvector type adapter on this connection."""
    register_vector(conn)


def upsert_kpis(conn: Connection[Any], definitions: list[KPIDefinition]) -> int:
    for kpi in definitions:
        conn.execute(
            """
            INSERT INTO app.kpis
                (kpi_code, domain, parameter, sub_parameter, variable_name, definition,
                 unit, data_type, formula, primary_source, benchmark_direction,
                 priority_12m, source_file, source_row)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (kpi_code) DO UPDATE SET
                domain = EXCLUDED.domain,
                parameter = EXCLUDED.parameter,
                sub_parameter = EXCLUDED.sub_parameter,
                variable_name = EXCLUDED.variable_name,
                definition = EXCLUDED.definition,
                unit = EXCLUDED.unit,
                data_type = EXCLUDED.data_type,
                formula = EXCLUDED.formula,
                primary_source = EXCLUDED.primary_source,
                benchmark_direction = EXCLUDED.benchmark_direction,
                priority_12m = EXCLUDED.priority_12m,
                source_file = EXCLUDED.source_file,
                source_row = EXCLUDED.source_row,
                loaded_at = now()
            """,
            (
                kpi.kpi_code,
                kpi.domain,
                kpi.parameter,
                kpi.sub_parameter,
                kpi.variable_name,
                kpi.definition,
                kpi.unit,
                kpi.data_type,
                kpi.formula,
                kpi.primary_source,
                kpi.benchmark_direction,
                kpi.priority_12m,
                kpi.source_file,
                Jsonb(kpi.source_row),
            ),
        )
    return len(definitions)


def upsert_kpi_embedding(
    conn: Connection[Any],
    *,
    kpi_code: str,
    representation: str,
    model_identifier: str,
    dimension: int,
    vector: list[float],
    normalized: bool,
    instruction: str | None,
    embedded_text: str,
) -> None:
    conn.execute(
        """
        INSERT INTO app.kpi_embeddings
            (kpi_code, representation, model_identifier, embedding_dimension,
             embedding, normalized, instruction, embedded_text_checksum)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (kpi_code, model_identifier, representation) DO UPDATE SET
            embedding_dimension = EXCLUDED.embedding_dimension,
            embedding = EXCLUDED.embedding,
            normalized = EXCLUDED.normalized,
            instruction = EXCLUDED.instruction,
            embedded_text_checksum = EXCLUDED.embedded_text_checksum,
            created_at = now()
        """,
        (
            kpi_code,
            representation,
            model_identifier,
            dimension,
            Vector(vector),
            normalized,
            instruction,
            text_checksum(embedded_text),
        ),
    )


def upsert_evidence_embedding(
    conn: Connection[Any],
    *,
    evidence_id: int,
    representation: str,
    model_identifier: str,
    dimension: int,
    vector: list[float],
    normalized: bool,
    instruction: str | None,
    canonical_serializer_version: str | None,
    embedded_text: str,
) -> None:
    conn.execute(
        """
        INSERT INTO app.evidence_embeddings
            (evidence_id, representation, model_identifier, embedding_dimension,
             embedding, normalized, instruction, canonical_serializer_version,
             embedded_text_checksum)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (evidence_id, representation, model_identifier) DO UPDATE SET
            embedding_dimension = EXCLUDED.embedding_dimension,
            embedding = EXCLUDED.embedding,
            normalized = EXCLUDED.normalized,
            instruction = EXCLUDED.instruction,
            canonical_serializer_version = EXCLUDED.canonical_serializer_version,
            embedded_text_checksum = EXCLUDED.embedded_text_checksum,
            created_at = now()
        """,
        (
            evidence_id,
            representation,
            model_identifier,
            dimension,
            Vector(vector),
            normalized,
            instruction,
            canonical_serializer_version,
            text_checksum(embedded_text),
        ),
    )


@dataclass(frozen=True)
class EvidenceRow:
    evidence_id: int
    artifact_id: int
    evidence_type: str
    canonical_text: str
    supporting_context: str
    canonical_serializer_version: str


def fetch_all_evidence(conn: Connection[Any]) -> list[EvidenceRow]:
    """Every evidence row currently in app.evidence — no filtering by type or anything else."""
    rows = conn.execute(
        """
        SELECT id, artifact_id, evidence_type, canonical_text, supporting_context,
               canonical_serializer_version
        FROM app.evidence
        ORDER BY id
        """
    ).fetchall()
    return [EvidenceRow(*row) for row in rows]


def count_evidence_by_type(conn: Connection[Any]) -> dict[str, int]:
    rows = conn.execute(
        "SELECT evidence_type, count(*) FROM app.evidence GROUP BY evidence_type ORDER BY evidence_type"
    ).fetchall()
    return {evidence_type: count for evidence_type, count in rows}


def get_evidence_embedding(
    conn: Connection[Any], evidence_id: int, *, representation: str, model_identifier: str
) -> list[float] | None:
    row = conn.execute(
        """
        SELECT embedding FROM app.evidence_embeddings
        WHERE evidence_id = %s AND representation = %s AND model_identifier = %s
        """,
        (evidence_id, representation, model_identifier),
    ).fetchone()
    return row[0].to_list() if row else None


def get_kpi_embedding(
    conn: Connection[Any], kpi_code: str, *, representation: str, model_identifier: str
) -> list[float] | None:
    row = conn.execute(
        """
        SELECT embedding FROM app.kpi_embeddings
        WHERE kpi_code = %s AND representation = %s AND model_identifier = %s
        """,
        (kpi_code, representation, model_identifier),
    ).fetchone()
    return row[0].to_list() if row else None


def index_stats(conn: Connection[Any]) -> dict[str, Any]:
    """Row counts and on-disk sizes for the vector tables (measured, not estimated)."""
    rows = conn.execute(
        """
        SELECT relname, n_live_tup, pg_size_pretty(pg_total_relation_size(('app.' || relname)::regclass))
        FROM pg_stat_user_tables
        WHERE schemaname = 'app' AND relname IN ('evidence_embeddings', 'kpi_embeddings')
        """
    ).fetchall()
    return {relname: {"row_estimate": n_live_tup, "total_size": size} for relname, n_live_tup, size in rows}


def top_k_kpis_for_vector(
    conn: Connection[Any],
    vector: list[float],
    *,
    model_identifier: str,
    representation: str,
    k: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT k.kpi_code, k.variable_name, k.definition, k.domain, k.parameter,
               (ke.embedding <=> %(vector)s) AS distance
        FROM app.kpi_embeddings ke
        JOIN app.kpis k ON k.kpi_code = ke.kpi_code
        WHERE ke.model_identifier = %(model)s AND ke.representation = %(representation)s
        ORDER BY ke.embedding <=> %(vector)s
        LIMIT %(k)s
        """,
        {"vector": Vector(vector), "model": model_identifier, "representation": representation, "k": k},
    ).fetchall()
    return [
        {
            "kpi_code": kpi_code,
            "variable_name": variable_name,
            "definition": definition,
            "domain": domain,
            "parameter": parameter,
            "distance": float(distance),
            "similarity": 1.0 - float(distance),
        }
        for kpi_code, variable_name, definition, domain, parameter, distance in rows
    ]


def top_k_evidence_for_vector(
    conn: Connection[Any],
    vector: list[float],
    *,
    model_identifier: str,
    representation: str,
    k: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.id, e.artifact_id, e.evidence_type, e.claim, e.value,
               (ee.embedding <=> %(vector)s) AS distance
        FROM app.evidence_embeddings ee
        JOIN app.evidence e ON e.id = ee.evidence_id
        WHERE ee.model_identifier = %(model)s AND ee.representation = %(representation)s
        ORDER BY ee.embedding <=> %(vector)s
        LIMIT %(k)s
        """,
        {"vector": Vector(vector), "model": model_identifier, "representation": representation, "k": k},
    ).fetchall()
    return [
        {
            "evidence_id": evidence_id,
            "artifact_id": artifact_id,
            "evidence_type": evidence_type,
            "claim": claim,
            "value": value,
            "distance": float(distance),
            "similarity": 1.0 - float(distance),
        }
        for evidence_id, artifact_id, evidence_type, claim, value, distance in rows
    ]
