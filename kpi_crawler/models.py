"""Storage models for acquisition records."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class AcquisitionRun:
    id: int
    status: str
    root_source_url: str | None
    started_at: datetime
    completed_at: datetime | None
    error_message: str | None


@dataclass(frozen=True)
class AcquisitionEvent:
    id: int
    run_id: int
    parent_event_id: int | None
    event_type: str
    status: str
    source_url: str | None
    source_type: str | None
    content_type: str | None
    retrieved_at: datetime | None
    error_message: str | None
    artifact_id: int | None
    occurred_at: datetime


@dataclass(frozen=True)
class Artifact:
    id: int
    sha256: str
    raw_storage_ref: str
    source_url: str | None
    source_type: str | None
    content_type: str | None
    retrieved_at: datetime
    byte_size: int
    created_at: datetime


@dataclass(frozen=True)
class Evidence:
    id: int
    artifact_id: int
    claim: str
    value: str
    supporting_context: str
    evidence_type: str
    structure: dict[str, Any]
    canonical_text: str
    canonical_serializer_version: str
    extraction_id: int | None
    created_at: datetime


@dataclass(frozen=True)
class Extraction:
    id: int
    artifact_id: int
    extractor: str
    extractor_version: str | None
    status: str
    metadata: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class EvidenceProvenance:
    evidence: Evidence
    artifact: Artifact
    source_url: str
    page_number: int | None
    location: str | None
    extractor: str
    extractor_version: str | None
    extracted_at: datetime
