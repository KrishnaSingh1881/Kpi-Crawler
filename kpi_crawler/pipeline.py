"""The smallest complete source-to-evidence pipeline, hardened against real-world failures.

Each run commits progressively at durable checkpoints (run created, artifact
acquired and stored, evidence persisted) so that a later-stage failure cannot
roll back and lose an artifact a prior stage already, successfully, preserved.

`_process_source` is the shared per-URL unit of work (acquire, store, extract
if the content type is supported) reused by both the single-URL pipeline here
and the multi-URL crawl orchestrator in `kpi_crawler.crawl`.
"""

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Literal

import psycopg

from .acquisition import (
    DEFAULT_MAX_ARTIFACT_BYTES,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    AcquisitionError,
    acquire_source,
)
from .db import connection
from .errors import (
    ApplicationError,
    DatabaseError,
    ExtractionError,
    OperationalLimitError,
    StorageError,
    UnsupportedContentError,
)
from .extraction import PdfExtractor
from .models import Artifact, EvidenceProvenance
from .storage import AcquisitionRepository


DEFAULT_ALLOWED_CONTENT_TYPES = frozenset({"application/pdf"})

logger = logging.getLogger(__name__)

FetchStatus = Literal[
    "acquired",  # stored and (if content type supported) extracted
    "duplicate_content",  # stored; extraction skipped, this content was already extracted
    "unsupported_content",  # stored; content type is not in allowed_content_types
    "acquisition_failed",  # not stored: fetch itself failed (network/HTTP/local read/size limit)
    "extraction_failed",  # stored; the extractor raised on this content
]


@dataclass(frozen=True)
class PipelineResult:
    artifact: Artifact
    evidence: tuple[EvidenceProvenance, ...]


@dataclass(frozen=True)
class FetchOutcome:
    """The result of processing one URL: what happened, and enough to keep discovering from it."""

    status: FetchStatus
    artifact: Artifact | None
    acquisition_event_id: int | None
    evidence: tuple[EvidenceProvenance, ...]
    content: bytes | None
    content_type: str | None
    resolved_url: str | None
    error: ApplicationError | None


def _record_failure(
    repository: AcquisitionRepository,
    conn: psycopg.Connection,
    run_id: int,
    event_type: str,
    source_url: str,
    error_message: str,
    *,
    parent_event_id: int | None = None,
    source_type: str | None = None,
    artifact_id: int | None = None,
) -> None:
    """Best-effort: record what happened. A secondary storage failure must not mask the original error."""
    try:
        repository.record_event(
            run_id,
            event_type,
            status="failed",
            parent_event_id=parent_event_id,
            source_url=source_url,
            source_type=source_type,
            artifact_id=artifact_id,
            error_message=error_message,
        )
        repository.complete_run(run_id, "failed", error_message)
        conn.commit()
    except Exception:
        logger.error("failed to record outcome for run %s", run_id, exc_info=True)


def _process_source(
    repository: AcquisitionRepository,
    conn: psycopg.Connection,
    run_id: int,
    source_url: str,
    storage_dir: Path,
    *,
    parent_event_id: int | None,
    timeout_seconds: float,
    retries: int,
    max_artifact_bytes: int,
    allowed_content_types: frozenset[str],
) -> FetchOutcome:
    """Acquire, store, and (if supported and not already extracted) extract one URL.

    Per-URL failures (acquisition, unsupported content, extraction) are recorded
    and returned as an outcome rather than raised, so a caller processing many
    URLs (a crawl) can continue. A storage failure (raw filesystem or database)
    is systemic rather than per-URL, so it is recorded and re-raised instead.

    Extraction is always skipped if this artifact's content was already
    successfully extracted before (by this call or an earlier one, in this run
    or a prior one) — content-addressed storage means the same bytes reached
    via two different URLs must not produce duplicate evidence.
    """
    try:
        acquired = acquire_source(
            source_url,
            storage_dir,
            timeout_seconds=timeout_seconds,
            retries=retries,
            max_bytes=max_artifact_bytes,
        )
    except (AcquisitionError, OperationalLimitError) as exc:
        _record_failure(repository, conn, run_id, "acquisition", source_url, str(exc), parent_event_id=parent_event_id)
        return FetchOutcome("acquisition_failed", None, None, (), None, None, None, exc)
    except StorageError as exc:
        _record_failure(repository, conn, run_id, "acquisition", source_url, str(exc), parent_event_id=parent_event_id)
        raise

    try:
        artifact = repository.store_artifact(
            acquired.content,
            acquired.raw_storage_ref,
            source_url=source_url,
            source_type=acquired.source_type,
            content_type=acquired.content_type,
            retrieved_at=acquired.retrieved_at,
        )
        acquisition_event = repository.record_event(
            run_id,
            "acquisition",
            status="succeeded",
            parent_event_id=parent_event_id,
            source_url=source_url,
            source_type=acquired.source_type,
            content_type=acquired.content_type,
            retrieved_at=acquired.retrieved_at,
            artifact_id=artifact.id,
        )
        conn.commit()
    except psycopg.Error as exc:
        conn.rollback()
        db_error = DatabaseError(f"failed to persist artifact for {source_url}: {exc}")
        _record_failure(repository, conn, run_id, "acquisition", source_url, str(db_error), parent_event_id=parent_event_id)
        raise db_error from exc

    if acquired.content_type not in allowed_content_types:
        message = f"unsupported content type: {acquired.content_type}"
        _record_failure(
            repository,
            conn,
            run_id,
            "extraction",
            source_url,
            message,
            parent_event_id=acquisition_event.id,
            artifact_id=artifact.id,
        )
        error = UnsupportedContentError(f"{source_url}: {message}")
        return FetchOutcome(
            "unsupported_content", artifact, acquisition_event.id, (), acquired.content,
            acquired.content_type, acquired.resolved_url, error,
        )

    if repository.artifact_has_extraction(artifact.id):
        return FetchOutcome(
            "duplicate_content", artifact, acquisition_event.id, (), acquired.content,
            acquired.content_type, acquired.resolved_url, None,
        )

    extractor = PdfExtractor()
    try:
        extraction_result = extractor.extract(artifact)
    except Exception as exc:
        extraction_error = ExtractionError(f"failed to extract {source_url}: {exc}")
        try:
            repository.create_extraction(
                artifact.id, extractor.name, extractor.version, "failed", {"error": str(exc)}
            )
        except psycopg.Error:
            conn.rollback()
        _record_failure(
            repository,
            conn,
            run_id,
            "extraction",
            source_url,
            str(exc),
            parent_event_id=acquisition_event.id,
            artifact_id=artifact.id,
        )
        return FetchOutcome(
            "extraction_failed", artifact, acquisition_event.id, (), acquired.content,
            acquired.content_type, acquired.resolved_url, extraction_error,
        )

    try:
        extraction = repository.create_extraction(
            artifact.id,
            extraction_result.extractor,
            extraction_result.extractor_version,
            "succeeded",
            extraction_result.metadata,
        )
        evidence = tuple(
            repository.create_evidence(
                artifact.id,
                item.claim,
                item.value,
                item.supporting_context,
                source_url=source_url,
                page_number=item.page_number,
                location=item.location,
                extractor=extraction_result.extractor,
                extractor_version=extraction_result.extractor_version,
                evidence_type=item.evidence_type,
                structure=item.structure,
                extraction_id=extraction.id,
            )
            for item in extraction_result.items
        )
        repository.record_event(
            run_id,
            "extraction",
            status="succeeded",
            parent_event_id=acquisition_event.id,
            source_url=source_url,
            source_type="pdf",
            artifact_id=artifact.id,
        )
        conn.commit()
    except psycopg.Error as exc:
        conn.rollback()
        db_error = DatabaseError(f"failed to persist evidence for {source_url}: {exc}")
        _record_failure(
            repository,
            conn,
            run_id,
            "extraction",
            source_url,
            str(db_error),
            parent_event_id=acquisition_event.id,
            artifact_id=artifact.id,
        )
        raise db_error from exc

    return FetchOutcome(
        "acquired", artifact, acquisition_event.id, evidence, acquired.content,
        acquired.content_type, acquired.resolved_url, None,
    )


def run_pdf_pipeline(
    source_url: str,
    database_url: str,
    storage_dir: Path,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    allowed_content_types: frozenset[str] = DEFAULT_ALLOWED_CONTENT_TYPES,
) -> PipelineResult:
    with connection(database_url) as conn:
        repository = AcquisitionRepository(conn)
        run = repository.create_run(source_url)
        conn.commit()

        outcome = _process_source(
            repository,
            conn,
            run.id,
            source_url,
            storage_dir,
            parent_event_id=None,
            timeout_seconds=timeout_seconds,
            retries=retries,
            max_artifact_bytes=max_artifact_bytes,
            allowed_content_types=allowed_content_types,
        )
        if outcome.error is not None:
            raise outcome.error

        repository.complete_run(run.id, "succeeded")
        conn.commit()
        return PipelineResult(outcome.artifact, outcome.evidence)
