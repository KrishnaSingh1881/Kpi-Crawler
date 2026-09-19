"""Human-readable Markdown report for one retrieval-baseline run.

The JSONL files written by the pipeline are the complete, authoritative
result set. This report is a summary over them plus a deterministic sample —
it never substitutes for the JSONL files and never hand-picks examples.
"""

from pathlib import Path
from typing import Any

from psycopg import Connection

from .config import RetrievalSettings
from .kpi_dictionary import KPIDefinition
from .pipeline import KPI_REPRESENTATION, REPRESENTATION_A, REPRESENTATION_B, RunOutcome
from .sampling import deterministic_evidence_sample, deterministic_kpi_sample
from .store import EvidenceRow, get_evidence_embedding, get_kpi_embedding, index_stats, top_k_evidence_for_vector, top_k_kpis_for_vector


def _fmt_seconds(value: float) -> str:
    return f"{value:.2f}s"


def _truncate(text: str, limit: int = 160) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render_evidence_sample_section(
    conn: Connection[Any],
    sample: list[EvidenceRow],
    *,
    model_identifier: str,
    top_k: int,
) -> list[str]:
    lines = ["### Evidence sample -> top-10 KPI (representation A vs B)", ""]
    for row in sample:
        lines.append(
            f"**Evidence {row.evidence_id}** (`{row.evidence_type}`, artifact {row.artifact_id})"
        )
        lines.append(f"- canonical_text: {_truncate(row.canonical_text)}")
        lines.append(f"- supporting_context: {_truncate(row.supporting_context)}")
        for label, representation in (("A (canonical_text)", REPRESENTATION_A), ("B (supporting_context)", REPRESENTATION_B)):
            vector = get_evidence_embedding(conn, row.evidence_id, representation=representation, model_identifier=model_identifier)
            if vector is None:
                lines.append(f"  - {label}: (no embedding found)")
                continue
            results = top_k_kpis_for_vector(
                conn, vector, model_identifier=model_identifier, representation=KPI_REPRESENTATION, k=min(top_k, 5)
            )
            top_str = "; ".join(f"{r['kpi_code']} ({r['similarity']:.3f})" for r in results)
            lines.append(f"  - {label} top-5: {top_str}")
        lines.append("")
    return lines


def _render_kpi_sample_section(
    conn: Connection[Any],
    sample: list[KPIDefinition],
    *,
    model_identifier: str,
    top_k: int,
) -> list[str]:
    lines = ["### KPI sample -> top-10 evidence (representation A vs B)", ""]
    for kpi in sample:
        lines.append(f"**{kpi.kpi_code}** — {kpi.variable_name}: {_truncate(kpi.definition, 120)}")
        vector = get_kpi_embedding(conn, kpi.kpi_code, representation=KPI_REPRESENTATION, model_identifier=model_identifier)
        if vector is None:
            lines.append("  - (no embedding found)")
            lines.append("")
            continue
        for label, representation in (("A (canonical_text)", REPRESENTATION_A), ("B (supporting_context)", REPRESENTATION_B)):
            results = top_k_evidence_for_vector(
                conn, vector, model_identifier=model_identifier, representation=representation, k=min(top_k, 5)
            )
            top_str = "; ".join(f"#{r['evidence_id']}/{r['evidence_type']} ({r['similarity']:.3f})" for r in results)
            lines.append(f"  - {label} top-5: {top_str}")
        lines.append("")
    return lines


def generate_report(
    conn: Connection[Any],
    outcome: RunOutcome,
    settings: RetrievalSettings,
) -> str:
    metrics = outcome.metrics
    model_identifier = metrics.embedding_config.model_identifier

    evidence_sample = deterministic_evidence_sample(outcome.evidence_rows, 30)
    kpi_sample = deterministic_kpi_sample(outcome.kpis, 20)

    lines: list[str] = []
    lines.append(f"# Retrieval Baseline Report — {metrics.run_id}")
    lines.append("")
    lines.append("Pure top-K cosine retrieval baseline: existing evidence, embedded as-is with a single "
                 "fixed model, stored in pgvector, queried both directions. No reranking, filtering, "
                 "thresholding, or relevance scoring of any kind. This is a measurement, not a mapping system.")
    lines.append("")

    lines.append("## 1. Run metadata")
    lines.append("")
    lines.append(f"- Run ID: `{metrics.run_id}`")
    lines.append(f"- Started (UTC): {metrics.started_at.isoformat()}")
    lines.append(f"- Finished (UTC): {metrics.finished_at.isoformat() if metrics.finished_at else '(unset)'}")
    lines.append(f"- KPI source file: `{metrics.kpi_source_file}`")
    lines.append(f"- Database: `{settings.database_url.split('@')[-1]}` (credentials redacted)")
    lines.append(f"- Retrieval K: {metrics.top_k}")
    lines.append("- Similarity metric: cosine (pgvector `<=>` cosine distance; similarity = 1 - distance)")
    lines.append("")

    lines.append("## 2-4. Model, embedding dimension, database/vector configuration")
    lines.append("")
    cfg = metrics.embedding_config
    lines.append(f"- Model identifier: `{cfg.model_identifier}`")
    lines.append("- Runtime: Ollama (local daemon, GGUF/llama.cpp backend), 100% GPU-resident on this machine")
    lines.append("- Underlying model: Qwen3-Embedding-4B, Q4_K_M GGUF quantization (~4.4 GB resident)")
    lines.append(
        "  - Reason for quantized GGUF instead of the raw bf16 safetensors: this machine's GPU has 8 GB "
        "total VRAM (~6.6 GB free at run time); the official bf16 weights alone need ~8 GB and do not fit. "
        "Ollama already had this exact model available as a Q4_K_M GGUF build, which is a runtime/quantization "
        "change, not a model substitution — still Qwen3-Embedding-4B."
    )
    lines.append(f"- Embedding endpoint: `{cfg.endpoint}` (local loopback only)")
    lines.append(f"- Embedding dimension: {cfg.expected_dimension}")
    lines.append(f"- Normalization applied: {cfg.normalize} (L2 normalization applied client-side after retrieval from the runtime)")
    lines.append(f"- Fixed embedding instruction: {cfg.instruction!r} (none — same for evidence and KPI text)")
    lines.append(f"- Batch size: {cfg.batch_size}")
    lines.append("- Vector storage: PostgreSQL 16 + pgvector (`pgvector/pgvector:pg16`), `vector(2560)` columns")
    lines.append("- Vector/index information: exact (brute-force) cosine search via `ORDER BY embedding <=> query LIMIT k`; "
                 "no ANN index (HNSW/IVFFlat) — at ~3.5K evidence rows and 229 KPI rows, an exact scan is fast and "
                 "removes approximate-index recall as a confound in this baseline measurement.")
    stats = index_stats(conn)
    for table, info in stats.items():
        lines.append(f"  - `app.{table}`: ~{info['row_estimate']} rows, {info['total_size']} on disk")
    lines.append("")

    lines.append("## 5-7. Corpus counts")
    lines.append("")
    lines.append(f"- KPI count (loaded from dictionary): {metrics.kpi_count}")
    lines.append(f"- Evidence count (all types, no filtering): {metrics.evidence_count}")
    lines.append("- Evidence count by type:")
    for evidence_type, count in sorted(metrics.evidence_by_type.items()):
        lines.append(f"  - `{evidence_type}`: {count}")
    lines.append("")

    lines.append("## 8-9. Timings")
    lines.append("")
    lines.append(f"- Embedding time, KPIs ({metrics.kpi_count} texts): {_fmt_seconds(metrics.embedding_time_kpi_s)}")
    lines.append(f"- Embedding time, evidence A / canonical_text ({metrics.evidence_count} texts): {_fmt_seconds(metrics.embedding_time_evidence_a_s)}")
    lines.append(f"- Embedding time, evidence B / supporting_context ({metrics.evidence_count} texts): {_fmt_seconds(metrics.embedding_time_evidence_b_s)}")
    total_embed = metrics.embedding_time_kpi_s + metrics.embedding_time_evidence_a_s + metrics.embedding_time_evidence_b_s
    lines.append(f"- Total embedding time: {_fmt_seconds(total_embed)}")
    total_texts = metrics.kpi_count + 2 * metrics.evidence_count
    lines.append(f"- Total texts embedded: {total_texts} ({total_embed / total_texts * 1000:.1f} ms/text average)" if total_texts else "")
    lines.append(f"- Retrieval time, evidence→KPI experiment ({metrics.evidence_to_kpi_queries} queries, A+B): {_fmt_seconds(metrics.retrieval_time_evidence_to_kpi_s)}")
    if metrics.evidence_to_kpi_queries:
        lines.append(f"  - {metrics.retrieval_time_evidence_to_kpi_s / metrics.evidence_to_kpi_queries * 1000:.2f} ms/query average")
    lines.append(f"- Retrieval time, KPI→evidence experiment ({metrics.kpi_to_evidence_queries} queries, A+B): {_fmt_seconds(metrics.retrieval_time_kpi_to_evidence_s)}")
    if metrics.kpi_to_evidence_queries:
        lines.append(f"  - {metrics.retrieval_time_kpi_to_evidence_s / metrics.kpi_to_evidence_queries * 1000:.2f} ms/query average")
    lines.append("")

    lines.append("## 10-11. Result row counts")
    lines.append("")
    lines.append(f"- `evidence_to_kpi_A.jsonl`: {metrics.evidence_to_kpi_a_rows} rows ({metrics.evidence_count} evidence x up to {metrics.top_k})")
    lines.append(f"- `evidence_to_kpi_B.jsonl`: {metrics.evidence_to_kpi_b_rows} rows")
    lines.append(f"- `kpi_to_evidence_A.jsonl`: {metrics.kpi_to_evidence_a_rows} rows ({metrics.kpi_count} KPIs x up to {metrics.top_k})")
    lines.append(f"- `kpi_to_evidence_B.jsonl`: {metrics.kpi_to_evidence_b_rows} rows")
    lines.append("")

    lines.append("## 12. Top-10 frequency summaries")
    lines.append("")
    lines.append("### KPI hit frequency within evidence→KPI (representation A), by evidence_type")
    lines.append("")
    for evidence_type, counter in sorted(metrics.kpi_frequency_by_evidence_type_a.items()):
        top = counter.most_common(10)
        lines.append(f"- `{evidence_type}` ({sum(counter.values())} total hits across top-{metrics.top_k}):")
        for kpi_code, count in top:
            lines.append(f"  - {kpi_code}: {count}")
    lines.append("")
    lines.append("### Evidence-type frequency within KPI→evidence top-10 results")
    lines.append("")
    for representation, counter in metrics.evidence_type_frequency_in_kpi_results.items():
        total = sum(counter.values())
        lines.append(f"- Representation {representation} ({total} total result rows):")
        for evidence_type, count in sorted(counter.items()):
            pct = (count / total * 100) if total else 0.0
            lines.append(f"  - `{evidence_type}`: {count} ({pct:.1f}%)")
    lines.append("")

    lines.append("## 13. Evidence sample (30, deterministic, stratified by evidence_type)")
    lines.append("")
    lines.extend(_render_evidence_sample_section(conn, evidence_sample, model_identifier=model_identifier, top_k=metrics.top_k))

    lines.append("## 14. KPI sample (20, deterministic, evenly spaced by kpi_code)")
    lines.append("")
    lines.extend(_render_kpi_sample_section(conn, kpi_sample, model_identifier=model_identifier, top_k=metrics.top_k))

    lines.append("## 16. Observations")
    lines.append("")
    lines.append(
        "See `observations.md` at the repository root for detailed, dated observations from this run. "
        "In short: this is a raw semantic-similarity baseline with no correctness guarantee — matches that "
        "look wrong are expected and are the point of measuring, not a defect in this phase's implementation."
    )
    lines.append("")

    lines.append("## 17. Complete result files")
    lines.append("")
    for name in ("evidence_to_kpi_A.jsonl", "evidence_to_kpi_B.jsonl", "kpi_to_evidence_A.jsonl", "kpi_to_evidence_B.jsonl"):
        lines.append(f"- `{name}`")
    lines.append("")

    return "\n".join(lines)


def write_report(conn: Connection[Any], outcome: RunOutcome, settings: RetrievalSettings) -> Path:
    report_text = generate_report(conn, outcome, settings)
    report_path = outcome.run_dir / "report.md"
    report_path.write_text(report_text, encoding="utf-8")
    return report_path
