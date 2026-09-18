"""Explicit repository access for acquisition storage."""

from datetime import datetime, timezone
import hashlib
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from .evidence_serialization import CANONICAL_SERIALIZER_VERSION, serialize_evidence
from .models import (
    AcquisitionEvent,
    AcquisitionRun,
    Artifact,
    Evidence,
    EvidenceProvenance,
    Extraction,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AcquisitionRepository:
    """Persistence operations for runs, events, and immutable artifacts."""

    def __init__(self, conn: Connection[Any]):
        self._conn = conn

    def create_run(self, root_source_url: str | None = None) -> AcquisitionRun:
        row = self._conn.execute(
            """
            INSERT INTO app.acquisition_runs (root_source_url)
            VALUES (%s)
            RETURNING id, status, root_source_url, started_at, completed_at, error_message
            """,
            (root_source_url,),
        ).fetchone()
        return AcquisitionRun(*row)

    def complete_run(
        self,
        run_id: int,
        status: str,
        error_message: str | None = None,
    ) -> AcquisitionRun:
        row = self._conn.execute(
            """
            UPDATE app.acquisition_runs
            SET status = %s, completed_at = now(), error_message = %s
            WHERE id = %s
            RETURNING id, status, root_source_url, started_at, completed_at, error_message
            """,
            (status, error_message, run_id),
        ).fetchone()
        if row is None:
            raise LookupError(f"acquisition run {run_id} does not exist")
        return AcquisitionRun(*row)

    def get_run(self, run_id: int) -> AcquisitionRun:
        row = self._conn.execute(
            """
            SELECT id, status, root_source_url, started_at, completed_at, error_message
            FROM app.acquisition_runs
            WHERE id = %s
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"acquisition run {run_id} does not exist")
        return AcquisitionRun(*row)

    def store_artifact(
        self,
        content: bytes,
        raw_storage_ref: str,
        *,
        source_url: str | None = None,
        source_type: str | None = None,
        content_type: str | None = None,
        retrieved_at: datetime | None = None,
    ) -> Artifact:
        """Insert content once; duplicate SHA-256 values return the original row."""
        sha256 = hashlib.sha256(content).hexdigest()
        row = self._conn.execute(
            """
            INSERT INTO app.artifacts
                (sha256, raw_storage_ref, source_url, source_type, content_type,
                 retrieved_at, byte_size)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sha256) DO NOTHING
            RETURNING id, sha256, raw_storage_ref, source_url, source_type,
                      content_type, retrieved_at, byte_size, created_at
            """,
            (
                sha256,
                raw_storage_ref,
                source_url,
                source_type,
                content_type,
                retrieved_at or _utc_now(),
                len(content),
            ),
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                """
                SELECT id, sha256, raw_storage_ref, source_url, source_type,
                       content_type, retrieved_at, byte_size, created_at
                FROM app.artifacts
                WHERE sha256 = %s
                """,
                (sha256,),
            ).fetchone()
        return Artifact(*row)

    def find_artifact(self, sha256: str) -> Artifact | None:
        row = self._conn.execute(
            """
            SELECT id, sha256, raw_storage_ref, source_url, source_type,
                   content_type, retrieved_at, byte_size, created_at
            FROM app.artifacts
            WHERE sha256 = %s
            """,
            (sha256,),
        ).fetchone()
        return Artifact(*row) if row else None

    def get_artifact(self, artifact_id: int) -> Artifact:
        row = self._conn.execute(
            """
            SELECT id, sha256, raw_storage_ref, source_url, source_type,
                   content_type, retrieved_at, byte_size, created_at
            FROM app.artifacts
            WHERE id = %s
            """,
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"artifact {artifact_id} does not exist")
        return Artifact(*row)

    def create_extraction(
        self,
        artifact_id: int,
        extractor: str,
        extractor_version: str | None,
        status: str,
        metadata: dict[str, Any],
    ) -> Extraction:
        row = self._conn.execute(
            """
            INSERT INTO app.extractions (artifact_id, extractor, extractor_version, status, metadata)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, artifact_id, extractor, extractor_version, status, metadata, created_at
            """,
            (artifact_id, extractor, extractor_version, status, Jsonb(metadata)),
        ).fetchone()
        return Extraction(*row)

    def artifact_has_extraction(self, artifact_id: int) -> bool:
        """Whether this artifact's content has already been successfully extracted, ever."""
        row = self._conn.execute(
            "SELECT 1 FROM app.extractions WHERE artifact_id = %s AND status = 'succeeded' LIMIT 1",
            (artifact_id,),
        ).fetchone()
        return row is not None

    def get_extraction(self, extraction_id: int) -> Extraction | None:
        row = self._conn.execute(
            """
            SELECT id, artifact_id, extractor, extractor_version, status, metadata, created_at
            FROM app.extractions WHERE id = %s
            """,
            (extraction_id,),
        ).fetchone()
        return Extraction(*row) if row else None

    def record_event(
        self,
        run_id: int,
        event_type: str,
        *,
        status: str,
        parent_event_id: int | None = None,
        source_url: str | None = None,
        source_type: str | None = None,
        content_type: str | None = None,
        retrieved_at: datetime | None = None,
        error_message: str | None = None,
        artifact_id: int | None = None,
    ) -> AcquisitionEvent:
        row = self._conn.execute(
            """
            INSERT INTO app.acquisition_events
                (run_id, parent_event_id, event_type, status, source_url,
                 source_type, content_type, retrieved_at, error_message, artifact_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, run_id, parent_event_id, event_type, status, source_url,
                      source_type, content_type, retrieved_at, error_message,
                      artifact_id, occurred_at
            """,
            (
                run_id,
                parent_event_id,
                event_type,
                status,
                source_url,
                source_type,
                content_type,
                retrieved_at,
                error_message,
                artifact_id,
            ),
        ).fetchone()
        return AcquisitionEvent(*row)

    def get_event_lineage(self, run_id: int) -> list[AcquisitionEvent]:
        rows = self._conn.execute(
            """
            WITH RECURSIVE lineage AS (
                SELECT e.*, ARRAY[e.id] AS path
                FROM app.acquisition_events e
                WHERE e.run_id = %s AND e.parent_event_id IS NULL
                UNION ALL
                SELECT child.*, lineage.path || child.id
                FROM app.acquisition_events child
                JOIN lineage ON lineage.id = child.parent_event_id
                WHERE child.run_id = %s
            )
            SELECT id, run_id, parent_event_id, event_type, status, source_url,
                   source_type, content_type, retrieved_at, error_message,
                   artifact_id, occurred_at
            FROM lineage
            ORDER BY path
            """,
            (run_id, run_id),
        ).fetchall()
        return [AcquisitionEvent(*row) for row in rows]

    def create_evidence(
        self,
        artifact_id: int,
        claim: str,
        value: str,
        supporting_context: str,
        *,
        source_url: str,
        page_number: int | None,
        location: str | None,
        extractor: str,
        extractor_version: str | None,
        evidence_type: str = "text",
        structure: dict[str, Any] | None = None,
        extraction_id: int | None = None,
        extracted_at: datetime | None = None,
    ) -> EvidenceProvenance:
        resolved_structure = structure or {}
        canonical_text = serialize_evidence(
            evidence_type, claim, value, supporting_context, resolved_structure
        )
        evidence_row = self._conn.execute(
            """
            INSERT INTO app.evidence
                (artifact_id, claim, value, supporting_context, evidence_type, structure,
                 canonical_text, canonical_serializer_version, extraction_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, artifact_id, claim, value, supporting_context,
                      evidence_type, structure, canonical_text, canonical_serializer_version,
                      extraction_id, created_at
            """,
            (
                artifact_id,
                claim,
                value,
                supporting_context,
                evidence_type,
                Jsonb(resolved_structure),
                canonical_text,
                CANONICAL_SERIALIZER_VERSION,
                extraction_id,
            ),
        ).fetchone()
        evidence = Evidence(*evidence_row)
        provenance_row = self._conn.execute(
            """
            INSERT INTO app.evidence_provenance
                (evidence_id, artifact_id, source_url, page_number, location,
                 extractor, extractor_version, extracted_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING source_url, page_number, location, extractor,
                      extractor_version, extracted_at
            """,
            (
                evidence.id,
                artifact_id,
                source_url,
                page_number,
                location,
                extractor,
                extractor_version,
                extracted_at or _utc_now(),
            ),
        ).fetchone()
        artifact = self.get_artifact(evidence.artifact_id)
        return EvidenceProvenance(evidence, artifact, *provenance_row)

    def get_evidence(self, evidence_id: int) -> EvidenceProvenance | None:
        row = self._conn.execute(
            """
            SELECT e.id, e.artifact_id, e.claim, e.value, e.supporting_context,
                   e.evidence_type, e.structure, e.canonical_text, e.canonical_serializer_version,
                   e.extraction_id, e.created_at,
                   a.id, a.sha256, a.raw_storage_ref, a.source_url,
                   a.source_type, a.content_type, a.retrieved_at, a.byte_size,
                   a.created_at, p.source_url, p.page_number, p.location,
                   p.extractor, p.extractor_version, p.extracted_at
            FROM app.evidence e
            JOIN app.artifacts a ON a.id = e.artifact_id
            JOIN app.evidence_provenance p ON p.evidence_id = e.id
            WHERE e.id = %s
            """,
            (evidence_id,),
        ).fetchone()
        if row is None:
            return None
        evidence = Evidence(*row[:11])
        artifact = Artifact(*row[11:20])
        return EvidenceProvenance(evidence, artifact, *row[20:])

    def list_evidence_for_artifact(self, artifact_id: int) -> list[EvidenceProvenance]:
        rows = self._conn.execute(
            """
            SELECT e.id, e.artifact_id, e.claim, e.value, e.supporting_context,
                   e.evidence_type, e.structure, e.canonical_text, e.canonical_serializer_version,
                   e.extraction_id, e.created_at,
                   a.id, a.sha256, a.raw_storage_ref, a.source_url,
                   a.source_type, a.content_type, a.retrieved_at, a.byte_size,
                   a.created_at, p.source_url, p.page_number, p.location,
                   p.extractor, p.extractor_version, p.extracted_at
            FROM app.evidence e
            JOIN app.artifacts a ON a.id = e.artifact_id
            JOIN app.evidence_provenance p ON p.evidence_id = e.id
            WHERE e.artifact_id = %s
            ORDER BY e.id
            """,
            (artifact_id,),
        ).fetchall()
        results = []
        for row in rows:
            evidence = Evidence(*row[:11])
            artifact = Artifact(*row[11:20])
            results.append(EvidenceProvenance(evidence, artifact, *row[20:]))
        return results
