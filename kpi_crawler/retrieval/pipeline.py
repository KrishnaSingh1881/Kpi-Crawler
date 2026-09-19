"""Orchestrates one retrieval-baseline run: embed, store, retrieve both
directions with both evidence representations, and write the raw JSONL
results. No filtering, reranking, or scoring beyond pgvector cosine distance.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from ..db import connection
from .config import RetrievalSettings
from .embedding_client import EmbeddingClient, EmbeddingConfig
from .kpi_dictionary import KPIDefinition, load_kpi_dictionary
from .store import (
    EvidenceRow,
    count_evidence_by_type,
    fetch_all_evidence,
    prepare_connection,
    top_k_evidence_for_vector,
    top_k_kpis_for_vector,
    upsert_evidence_embedding,
    upsert_kpi_embedding,
    upsert_kpis,
)

REPRESENTATION_A = "canonical_text"
REPRESENTATION_B = "supporting_context"
KPI_REPRESENTATION = "name_and_definition"


@dataclass
class RunMetrics:
    run_id: str
    started_at: datetime
    kpi_source_file: str
    embedding_config: EmbeddingConfig
    top_k: int
    kpi_count: int = 0
    evidence_count: int = 0
    evidence_by_type: dict[str, int] = field(default_factory=dict)
    embedding_time_kpi_s: float = 0.0
    embedding_time_evidence_a_s: float = 0.0
    embedding_time_evidence_b_s: float = 0.0
    retrieval_time_evidence_to_kpi_s: float = 0.0
    retrieval_time_kpi_to_evidence_s: float = 0.0
    evidence_to_kpi_a_rows: int = 0
    evidence_to_kpi_b_rows: int = 0
    kpi_to_evidence_a_rows: int = 0
    kpi_to_evidence_b_rows: int = 0
    evidence_to_kpi_queries: int = 0
    kpi_to_evidence_queries: int = 0
    kpi_frequency_by_evidence_type_a: dict[str, Counter] = field(default_factory=dict)
    evidence_type_frequency_in_kpi_results: dict[str, Counter] = field(default_factory=dict)
    finished_at: datetime | None = None


@dataclass(frozen=True)
class RunOutcome:
    run_dir: Path
    metrics: RunMetrics
    kpis: list[KPIDefinition]
    evidence_rows: list[EvidenceRow]


def run_baseline(settings: RetrievalSettings) -> RunOutcome:
    run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    # Load and validate the KPI dictionary before creating any output directory or
    # touching the database, so a "fail loudly" error (missing/malformed file)
    # never leaves a half-created, empty run directory behind.
    kpis = load_kpi_dictionary(settings.kpi_dictionary_path)

    run_dir = settings.reports_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    embedding_config = EmbeddingConfig(
        model_identifier=settings.embedding_model,
        endpoint=settings.embedding_endpoint,
        expected_dimension=settings.embedding_dimension,
    )
    client = EmbeddingClient(embedding_config)

    metrics = RunMetrics(
        run_id=run_id,
        started_at=datetime.now(timezone.utc),
        kpi_source_file=str(settings.kpi_dictionary_path),
        embedding_config=embedding_config,
        top_k=settings.top_k,
        kpi_count=len(kpis),
    )

    with connection(settings.database_url) as conn:
        prepare_connection(conn)
        with conn.transaction():
            upsert_kpis(conn, kpis)

        evidence_rows = fetch_all_evidence(conn)
        metrics.evidence_count = len(evidence_rows)
        metrics.evidence_by_type = count_evidence_by_type(conn)

        # --- Embed KPIs (single fixed representation: name + definition) ---
        kpi_texts = [kpi.embedding_text for kpi in kpis]
        t0 = time.monotonic()
        kpi_vectors = client.embed_all(kpi_texts)
        metrics.embedding_time_kpi_s = time.monotonic() - t0
        with conn.transaction():
            for kpi, vector in zip(kpis, kpi_vectors):
                upsert_kpi_embedding(
                    conn,
                    kpi_code=kpi.kpi_code,
                    representation=KPI_REPRESENTATION,
                    model_identifier=embedding_config.model_identifier,
                    dimension=embedding_config.expected_dimension,
                    vector=vector,
                    normalized=embedding_config.normalize,
                    instruction=embedding_config.instruction,
                    embedded_text=kpi.embedding_text,
                )

        # --- Embed evidence representation A: the exact persisted canonical_text ---
        texts_a = [row.canonical_text for row in evidence_rows]
        t0 = time.monotonic()
        vectors_a = client.embed_all(texts_a)
        metrics.embedding_time_evidence_a_s = time.monotonic() - t0
        with conn.transaction():
            for row, vector in zip(evidence_rows, vectors_a):
                upsert_evidence_embedding(
                    conn,
                    evidence_id=row.evidence_id,
                    representation=REPRESENTATION_A,
                    model_identifier=embedding_config.model_identifier,
                    dimension=embedding_config.expected_dimension,
                    vector=vector,
                    normalized=embedding_config.normalize,
                    instruction=embedding_config.instruction,
                    canonical_serializer_version=row.canonical_serializer_version,
                    embedded_text=row.canonical_text,
                )

        # --- Embed evidence representation B: the exact persisted supporting_context ---
        texts_b = [row.supporting_context for row in evidence_rows]
        t0 = time.monotonic()
        vectors_b = client.embed_all(texts_b)
        metrics.embedding_time_evidence_b_s = time.monotonic() - t0
        with conn.transaction():
            for row, vector in zip(evidence_rows, vectors_b):
                upsert_evidence_embedding(
                    conn,
                    evidence_id=row.evidence_id,
                    representation=REPRESENTATION_B,
                    model_identifier=embedding_config.model_identifier,
                    dimension=embedding_config.expected_dimension,
                    vector=vector,
                    normalized=embedding_config.normalize,
                    instruction=embedding_config.instruction,
                    canonical_serializer_version=row.canonical_serializer_version,
                    embedded_text=row.supporting_context,
                )

        # --- Experiment A: evidence -> KPI (all 229 KPIs eligible, no filtering) ---
        kpi_freq_a: dict[str, Counter] = {}
        t0 = time.monotonic()
        with (run_dir / "evidence_to_kpi_A.jsonl").open("w", encoding="utf-8") as fa, (
            run_dir / "evidence_to_kpi_B.jsonl"
        ).open("w", encoding="utf-8") as fb:
            for row, vec_a, vec_b in zip(evidence_rows, vectors_a, vectors_b):
                for representation, vector, handle in (
                    (REPRESENTATION_A, vec_a, fa),
                    (REPRESENTATION_B, vec_b, fb),
                ):
                    metrics.evidence_to_kpi_queries += 1
                    results = top_k_kpis_for_vector(
                        conn,
                        vector,
                        model_identifier=embedding_config.model_identifier,
                        representation=KPI_REPRESENTATION,
                        k=settings.top_k,
                    )
                    for rank, result in enumerate(results, start=1):
                        handle.write(
                            json.dumps(
                                {
                                    "evidence_id": row.evidence_id,
                                    "artifact_id": row.artifact_id,
                                    "evidence_type": row.evidence_type,
                                    "query_representation": representation,
                                    "model_identifier": embedding_config.model_identifier,
                                    "top_k": settings.top_k,
                                    "rank": rank,
                                    "kpi_code": result["kpi_code"],
                                    "variable_name": result["variable_name"],
                                    "domain": result["domain"],
                                    "parameter": result["parameter"],
                                    "similarity": result["similarity"],
                                    "distance": result["distance"],
                                }
                            )
                            + "\n"
                        )
                        if representation == REPRESENTATION_A:
                            metrics.evidence_to_kpi_a_rows += 1
                            kpi_freq_a.setdefault(row.evidence_type, Counter())[result["kpi_code"]] += 1
                        else:
                            metrics.evidence_to_kpi_b_rows += 1
        metrics.retrieval_time_evidence_to_kpi_s = time.monotonic() - t0
        metrics.kpi_frequency_by_evidence_type_a = kpi_freq_a

        # --- Experiment B: KPI -> evidence (all evidence rows eligible, no filtering) ---
        evidence_type_freq: dict[str, Counter] = {REPRESENTATION_A: Counter(), REPRESENTATION_B: Counter()}
        t0 = time.monotonic()
        with (run_dir / "kpi_to_evidence_A.jsonl").open("w", encoding="utf-8") as fa, (
            run_dir / "kpi_to_evidence_B.jsonl"
        ).open("w", encoding="utf-8") as fb:
            for kpi, vector in zip(kpis, kpi_vectors):
                for representation, handle in ((REPRESENTATION_A, fa), (REPRESENTATION_B, fb)):
                    metrics.kpi_to_evidence_queries += 1
                    results = top_k_evidence_for_vector(
                        conn,
                        vector,
                        model_identifier=embedding_config.model_identifier,
                        representation=representation,
                        k=settings.top_k,
                    )
                    for rank, result in enumerate(results, start=1):
                        handle.write(
                            json.dumps(
                                {
                                    "kpi_code": kpi.kpi_code,
                                    "variable_name": kpi.variable_name,
                                    "domain": kpi.domain,
                                    "target_representation": representation,
                                    "model_identifier": embedding_config.model_identifier,
                                    "top_k": settings.top_k,
                                    "rank": rank,
                                    "evidence_id": result["evidence_id"],
                                    "artifact_id": result["artifact_id"],
                                    "evidence_type": result["evidence_type"],
                                    "claim": result["claim"],
                                    "value": result["value"],
                                    "similarity": result["similarity"],
                                    "distance": result["distance"],
                                }
                            )
                            + "\n"
                        )
                        evidence_type_freq[representation][result["evidence_type"]] += 1
                        if representation == REPRESENTATION_A:
                            metrics.kpi_to_evidence_a_rows += 1
                        else:
                            metrics.kpi_to_evidence_b_rows += 1
        metrics.retrieval_time_kpi_to_evidence_s = time.monotonic() - t0
        metrics.evidence_type_frequency_in_kpi_results = evidence_type_freq

    metrics.finished_at = datetime.now(timezone.utc)
    return RunOutcome(run_dir=run_dir, metrics=metrics, kpis=kpis, evidence_rows=evidence_rows)
