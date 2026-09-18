"""Export a completed acquisition run as a self-contained, human-readable package.

This is a read-only presentation snapshot of what is already in PostgreSQL and
raw storage: PostgreSQL remains the canonical structured store, and the raw
artifact files remain the canonical acquired files. Export never writes to
PostgreSQL, never re-runs acquisition or extraction, and never transforms a
stored evidence record — it serializes the persisted rows as they are. It adds
no relevance scoring, ranking, or evidence intelligence of any kind.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import mimetypes
from pathlib import Path
import shutil
import tempfile

from .db import connection
from .errors import ExportError
from .models import AcquisitionEvent, AcquisitionRun, Artifact, EvidenceProvenance
from .storage import AcquisitionRepository


_EXTENSION_OVERRIDES = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "text/plain": ".txt",
    "application/xml": ".xml",
    "text/xml": ".xml",
}


@dataclass(frozen=True)
class ExportResult:
    run_id: int
    output_dir: Path
    source_count: int
    evidence_count: int
    rejected_count: int


def _extension_for(content_type: str | None) -> str:
    if not content_type:
        return ".bin"
    return _EXTENSION_OVERRIDES.get(content_type) or mimetypes.guess_extension(content_type) or ".bin"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _artifact_json(artifact: Artifact) -> dict:
    return {
        "artifact_id": artifact.id,
        "sha256": artifact.sha256,
        "content_type": artifact.content_type,
        "byte_size": artifact.byte_size,
        "retrieved_at": _iso(artifact.retrieved_at),
        "created_at": _iso(artifact.created_at),
    }


def _evidence_json(item: EvidenceProvenance) -> dict:
    evidence = item.evidence
    return {
        "evidence_id": evidence.id,
        "artifact_id": evidence.artifact_id,
        "evidence_type": evidence.evidence_type,
        "claim": evidence.claim,
        "value": evidence.value,
        "supporting_context": evidence.supporting_context,
        "structure": evidence.structure,
        "canonical_text": evidence.canonical_text,
        "canonical_serializer_version": evidence.canonical_serializer_version,
        "source_url": item.source_url,
        "page_number": item.page_number,
        "location": item.location,
        "extractor": item.extractor,
        "extractor_version": item.extractor_version,
        "extracted_at": _iso(item.extracted_at),
        "created_at": _iso(evidence.created_at),
    }


def _event_json(event: AcquisitionEvent) -> dict:
    return {
        "event_id": event.id,
        "parent_event_id": event.parent_event_id,
        "event_type": event.event_type,
        "status": event.status,
        "source_url": event.source_url,
        "content_type": event.content_type,
        "error_message": event.error_message,
        "occurred_at": _iso(event.occurred_at),
    }


def _copy_original(artifact: Artifact, source_dir: Path) -> dict:
    """Copy the raw artifact byte-for-byte; report clearly if it cannot be included."""
    filename = f"original{_extension_for(artifact.content_type)}"
    raw_path = Path(artifact.raw_storage_ref)
    if not raw_path.is_file():
        return {"included": False, "filename": None, "reason": f"raw artifact file not found at {raw_path}"}
    try:
        shutil.copyfile(raw_path, source_dir / filename)
    except OSError as exc:
        return {"included": False, "filename": None, "reason": f"failed to copy raw artifact: {exc}"}
    return {"included": True, "filename": filename, "reason": None}


def _render_readme(run: AcquisitionRun, summary: dict, sources: list[dict], rejected: list[dict]) -> str:
    lines: list[str] = []
    lines.append(f"# Acquisition Run {run.id} — Evidence Export")
    lines.append("")
    lines.append(
        "This folder is a self-contained, human-readable snapshot of one acquisition/crawl "
        "run, exported for review and demonstration outside the database. The system's "
        "PostgreSQL database remains the authoritative record; this export is a copy."
    )
    lines.append("")
    lines.append("## Run details")
    lines.append("")
    lines.append(f"- Root/source URL: {run.root_source_url or '(none)'}")
    lines.append(f"- Run ID: {run.id}")
    lines.append(f"- Started: {run.started_at}")
    lines.append(f"- Completed: {run.completed_at or '(not completed)'}")
    lines.append(f"- Status: {run.status}")
    if run.error_message:
        lines.append(f"- Error: {run.error_message}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Resources discovered: {summary['resources_discovered']}")
    lines.append(f"- Resources acquired: {summary['resources_acquired']}")
    lines.append(f"- Resources rejected or failed: {summary['resources_rejected_or_failed']}")
    lines.append(f"- Distinct sources included in this export: {summary['distinct_sources_exported']}")
    lines.append("- Artifacts by type:")
    if summary["artifacts_by_type"]:
        for content_type, count in sorted(summary["artifacts_by_type"].items()):
            lines.append(f"  - {content_type}: {count}")
    else:
        lines.append("  - (none)")
    lines.append(
        f"- Extraction: {summary['extraction_succeeded']} succeeded, "
        f"{summary['extraction_no_evidence']} produced no evidence"
    )
    lines.append(f"- Total evidence records: {summary['evidence_total']}")
    lines.append("")
    lines.append("## What's in this export")
    lines.append("")
    lines.append("Each folder under `sources/` is one acquired document or web resource. It contains:")
    lines.append("")
    lines.append("- `source.json` — where this content came from and what happened to it")
    lines.append(
        "- `original.<ext>` — the original file exactly as acquired, byte-for-byte identical "
        "to the copy the system stores internally"
    )
    lines.append(
        "- `evidence.jsonl` — one line per extracted evidence record (this can be empty, if "
        "this resource's content type was not supported for extraction, or extraction failed)"
    )
    lines.append("")
    lines.append("`manifest.json` holds the same information in one machine-readable file.")
    lines.append("")
    lines.append("## What is an evidence record?")
    lines.append("")
    lines.append(
        "An evidence record is one fact found in a source document: a claim, its value, and "
        "the surrounding text it was found in (\"supporting context\"), plus a short plain-text "
        "summary (\"canonical text\"). Every evidence record can be traced back to its exact "
        "source: its `source_url`, and — for PDF pages — the `page_number` and `location` "
        "(bounding box on the page) it was found at, all recorded alongside it in "
        "`evidence.jsonl`. The `artifact_id` on each evidence record matches the "
        "`sources/<artifact_id>/` folder it came from, which holds the original document itself."
    )
    lines.append("")
    lines.append("## Sources included")
    lines.append("")
    if sources:
        for entry in sources:
            urls = ", ".join(entry["source_urls"]) or "(no source URL recorded)"
            note = "" if entry["original_artifact"]["included"] else " — original file unavailable"
            lines.append(
                f"- `sources/{entry['artifact_id']}/` — {urls} "
                f"({entry['content_type']}, {entry['evidence_count']} evidence records){note}"
            )
    else:
        lines.append("(none — nothing was successfully acquired in this run)")
    lines.append("")
    lines.append("## Rejected or failed resources (not included under `sources/`)")
    lines.append("")
    if rejected:
        for entry in rejected:
            lines.append(f"- {entry['source_url']} — {entry['error_message']}")
    else:
        lines.append("(none)")
    lines.append("")
    return "\n".join(lines)


def export_run(run_id: int, database_url: str, export_dir: Path) -> ExportResult:
    """Export a run's persisted artifacts and evidence as a human-readable package.

    Read-only: never writes to PostgreSQL. Safe to re-run — the package is built
    in a temporary directory and only atomically swapped into place once it is
    complete, so a failed or repeated export never leaves a half-written or
    corrupted package at the final path.
    """
    with connection(database_url) as conn:
        repository = AcquisitionRepository(conn)
        try:
            run = repository.get_run(run_id)
        except LookupError as exc:
            raise ExportError(f"run {run_id} does not exist") from exc

        events = repository.get_event_lineage(run_id)

        succeeded_acquisitions = [e for e in events if e.event_type == "acquisition" and e.status == "succeeded"]
        failed_acquisitions = [e for e in events if e.event_type == "acquisition" and e.status == "failed"]
        rejected_discoveries = [e for e in events if e.event_type == "discovery" and e.status == "failed"]

        artifact_ids = sorted({e.artifact_id for e in succeeded_acquisitions if e.artifact_id is not None})

        sources = []
        for artifact_id in artifact_ids:
            sources.append(
                {
                    "artifact": repository.get_artifact(artifact_id),
                    "evidence": repository.list_evidence_for_artifact(artifact_id),
                    "acquisition_events": [e for e in succeeded_acquisitions if e.artifact_id == artifact_id],
                    "extraction_events": [
                        e for e in events if e.event_type == "extraction" and e.artifact_id == artifact_id
                    ],
                }
            )

    # Everything needed is now in memory; the rest is pure file I/O, no further DB access.
    discovered_urls = (
        {e.source_url for e in succeeded_acquisitions}
        | {e.source_url for e in failed_acquisitions}
        | {e.source_url for e in rejected_discoveries}
    )

    export_dir.mkdir(parents=True, exist_ok=True)
    final_path = export_dir / f"run-{run_id}"
    temp_path = Path(tempfile.mkdtemp(dir=export_dir, prefix=f".run-{run_id}.tmp-"))
    try:
        sources_dir = temp_path / "sources"
        sources_dir.mkdir()

        source_manifest_entries = []
        evidence_total = 0
        for source in sources:
            artifact: Artifact = source["artifact"]
            evidence_list: list[EvidenceProvenance] = source["evidence"]
            source_dir = sources_dir / str(artifact.id)
            source_dir.mkdir()

            original_info = _copy_original(artifact, source_dir)

            with (source_dir / "evidence.jsonl").open("w", encoding="utf-8") as handle:
                for item in evidence_list:
                    handle.write(json.dumps(_evidence_json(item)) + "\n")

            source_json = {
                "run_id": run_id,
                **_artifact_json(artifact),
                "source_urls": [_event_json(e) for e in source["acquisition_events"]],
                "extraction_events": [_event_json(e) for e in source["extraction_events"]],
                "evidence_count": len(evidence_list),
                "original_artifact": original_info,
            }
            (source_dir / "source.json").write_text(json.dumps(source_json, indent=2), encoding="utf-8")

            source_manifest_entries.append(
                {
                    **_artifact_json(artifact),
                    "source_urls": [e.source_url for e in source["acquisition_events"]],
                    "evidence_count": len(evidence_list),
                    "extraction_status": "succeeded" if evidence_list else "no_evidence",
                    "original_artifact": original_info,
                }
            )
            evidence_total += len(evidence_list)

        rejected_entries = [_event_json(e) for e in (failed_acquisitions + rejected_discoveries)]

        artifacts_by_type = Counter(source["artifact"].content_type or "unknown" for source in sources)

        summary = {
            "resources_discovered": len(discovered_urls),
            "resources_acquired": len(succeeded_acquisitions),
            "resources_rejected_or_failed": len(failed_acquisitions) + len(rejected_discoveries),
            "distinct_sources_exported": len(sources),
            "artifacts_by_type": dict(sorted(artifacts_by_type.items())),
            "extraction_succeeded": sum(1 for s in source_manifest_entries if s["evidence_count"] > 0),
            "extraction_no_evidence": sum(1 for s in source_manifest_entries if s["evidence_count"] == 0),
            "evidence_total": evidence_total,
        }

        manifest = {
            "run": {
                "id": run.id,
                "root_source_url": run.root_source_url,
                "status": run.status,
                "started_at": _iso(run.started_at),
                "completed_at": _iso(run.completed_at),
                "error_message": run.error_message,
            },
            "summary": summary,
            "sources": source_manifest_entries,
            "rejected": rejected_entries,
            "exported_at": _iso(datetime.now(timezone.utc)),
        }
        (temp_path / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (temp_path / "README.md").write_text(
            _render_readme(run, summary, source_manifest_entries, rejected_entries), encoding="utf-8"
        )

        if final_path.exists():
            shutil.rmtree(final_path)
        temp_path.replace(final_path)
    except Exception:
        shutil.rmtree(temp_path, ignore_errors=True)
        raise

    return ExportResult(
        run_id=run_id,
        output_dir=final_path,
        source_count=len(sources),
        evidence_count=evidence_total,
        rejected_count=len(rejected_entries),
    )
