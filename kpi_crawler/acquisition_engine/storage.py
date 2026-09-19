"""Run-scoped, isolated storage for Program 1 acquisition runs.

Ensures every run is completely self-contained and reproducible under:
.data/
└── runs/
    └── <safe-site-id>/
        └── <run-id>/
            ├── raw/
            │   ├── html/
            │   ├── json/
            │   ├── pdf/
            │   └── other/
            ├── evidence/
            │   ├── attempts.jsonl
            │   └── adaptive_decisions.jsonl
            ├── manifest.json
            └── program2/
                ├── artifacts.json
                ├── metadata.json
                └── provenance.json
"""

from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from ..errors import StorageError
from .contract import (
    CONTRACT_VERSION,
    AdaptiveDecisionRecord,
    ArtifactRecord,
    AttemptRecord,
    RunSummary,
)


def safe_site_id(root_url: str) -> str:
    """Derive a safe, human-readable filesystem identifier from a root URL.

    Replaces colons and special characters with underscores, folds to lowercase,
    and strips leading/trailing dots/underscores.
    """
    parts = urlsplit(root_url)
    netloc = parts.netloc.lower()
    if not netloc:
        netloc = parts.path.strip("/").replace("/", "_") or "local"
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", netloc).strip("._")
    return safe or "unknown_site"


def classify_content_type(content_type: str | None, url: str = "") -> str:
    """Classify an artifact into one of the four required raw categories:

    'html', 'json', 'pdf', or 'other'.
    """
    ct = (content_type or "").lower().split(";")[0].strip()
    url_lower = url.lower().split("?")[0]
    if "html" in ct or url_lower.endswith((".html", ".htm")):
        return "html"
    if "json" in ct or url_lower.endswith(".json"):
        return "json"
    if "pdf" in ct or url_lower.endswith(".pdf"):
        return "pdf"
    return "other"


def _iso(val: datetime | None) -> str | None:
    return val.isoformat() if val is not None else None


class RunStorage:
    """Manages the isolated filesystem hierarchy for a single acquisition run."""

    def __init__(self, base_dir: Path, site_id: str, run_id: int):
        self.base_dir = Path(base_dir)
        self.site_id = site_id
        self.run_id = run_id

        runs_root = self.base_dir if self.base_dir.name == "runs" else self.base_dir / "runs"
        self.run_dir = (runs_root / self.site_id / str(self.run_id)).resolve()

        self.raw_dir = self.run_dir / "raw"
        self.raw_html_dir = self.raw_dir / "html"
        self.raw_json_dir = self.raw_dir / "json"
        self.raw_pdf_dir = self.raw_dir / "pdf"
        self.raw_other_dir = self.raw_dir / "other"
        self.evidence_dir = self.run_dir / "evidence"
        self.program2_dir = self.run_dir / "program2"

        self._init_dirs()

    def _init_dirs(self) -> None:
        self.raw_html_dir.mkdir(parents=True, exist_ok=True)
        self.raw_json_dir.mkdir(parents=True, exist_ok=True)
        self.raw_pdf_dir.mkdir(parents=True, exist_ok=True)
        self.raw_other_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.program2_dir.mkdir(parents=True, exist_ok=True)

    def write_raw_artifact(
        self,
        content: bytes,
        checksum: str,
        content_type: str | None = None,
        url: str = "",
    ) -> Path:
        """Atomically persist a raw artifact into its classified raw subdirectory.

        Guarantees that artifacts cannot escape their run directory.
        """
        category = classify_content_type(content_type, url)
        target_dir = self.raw_dir / category
        path = (target_dir / f"{checksum}.raw").resolve()

        # Path containment guard: artifact cannot escape its run directory
        try:
            path.relative_to(self.run_dir)
        except ValueError as exc:
            raise StorageError(
                f"security violation: artifact path {path} escapes run directory {self.run_dir}"
            ) from exc

        if path.exists():
            return path

        temporary_path: Path | None = None
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target_dir, delete=False) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise StorageError(f"failed to persist raw artifact {checksum}: {exc}") from exc

        return path

    def write_evidence(
        self,
        attempts: list[AttemptRecord],
        decisions: list[AdaptiveDecisionRecord],
    ) -> None:
        """Write run-scoped evidence ledger rows to disk in JSONL format."""
        attempts_path = self.evidence_dir / "attempts.jsonl"
        with attempts_path.open("w", encoding="utf-8") as f:
            for a in attempts:
                record = {
                    "attempt_id": a.attempt_id,
                    "run_id": a.run_id,
                    "occurred_at": _iso(a.occurred_at),
                    "url": a.url,
                    "domain": a.domain,
                    "acquisition_method": a.acquisition_method,
                    "session_id": a.session_id,
                    "proxy_id": a.proxy_id,
                    "proxy_status": a.proxy_status,
                    "http_status": a.http_status,
                    "latency_ms": a.latency_ms,
                    "retry_number": a.retry_number,
                    "retry_budget": a.retry_budget,
                    "timeout_seconds": a.timeout_seconds,
                    "backoff_applied_seconds": a.backoff_applied_seconds,
                    "concurrency_at_attempt": a.concurrency_at_attempt,
                    "rate_limit_detected": a.rate_limit_detected,
                    "failure_classification": a.failure_classification,
                    "adaptive_decision": a.adaptive_decision,
                    "final_result": a.final_result.value,
                    "artifact_id": a.artifact_id,
                    "error_message": a.error_message,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        decisions_path = self.evidence_dir / "adaptive_decisions.jsonl"
        with decisions_path.open("w", encoding="utf-8") as f:
            for d in decisions:
                record = {
                    "decision_id": d.decision_id,
                    "run_id": d.run_id,
                    "occurred_at": _iso(d.occurred_at),
                    "domain": d.domain,
                    "decision_type": d.decision_type,
                    "reason": d.reason,
                    "before_value": d.before_value,
                    "after_value": d.after_value,
                    "details": d.details,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write_program2_handoff(
        self,
        artifacts: list[ArtifactRecord],
        summary: RunSummary,
    ) -> None:
        """Generate the clean Program-2 handoff package inside program2/.

        Does not leak Crawlee/Playwright/session/proxy internals.
        """
        # 1. program2/artifacts.json
        artifacts_data = [
            {
                "contract_version": a.contract_version,
                "artifact_id": a.artifact_id,
                "run_id": a.run_id,
                "source_url": a.source_url,
                "canonical_url": a.canonical_url,
                "discovered_from": a.discovered_from,
                "fetched_at": _iso(a.fetched_at),
                "content_type": a.content_type,
                "http_status": a.http_status,
                "acquisition_method": a.acquisition_method,
                "raw_location": a.raw_location,
                "content_size": a.content_size,
                "checksum": a.checksum,
                "encoding": a.encoding,
                "final_url": a.final_url,
                "status": a.status.value,
            }
            for a in artifacts
        ]
        (self.program2_dir / "artifacts.json").write_text(
            json.dumps(artifacts_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # 2. program2/metadata.json
        categories = Counter(classify_content_type(a.content_type, a.source_url) for a in artifacts)
        metadata = {
            "contract_version": summary.contract_version,
            "run_id": summary.run_id,
            "site_id": self.site_id,
            "root_source_url": summary.root_source_url,
            "started_at": _iso(summary.started_at),
            "completed_at": _iso(summary.completed_at),
            "status": summary.status.value,
            "artifact_count": len(artifacts),
            "artifacts_by_category": dict(categories),
            "discovered": summary.discovered,
            "attempted": summary.attempted,
            "acquired": summary.acquired,
            "failed": summary.failed,
            "retries": summary.retries,
            "rate_limited": summary.rate_limited,
            "browser_pages": summary.browser_pages,
            "proxy_failures": summary.proxy_failures,
        }
        (self.program2_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # 3. program2/provenance.json
        provenance = {
            "contract_version": summary.contract_version,
            "run_id": summary.run_id,
            "site_id": self.site_id,
            "root_source_url": summary.root_source_url,
            "artifacts": [
                {
                    "artifact_id": a.artifact_id,
                    "source_url": a.source_url,
                    "canonical_url": a.canonical_url,
                    "discovered_from": a.discovered_from,
                    "redirect_chain": [{"url": hop.url, "status": hop.status} for hop in a.redirect_chain],
                    "fetched_at": _iso(a.fetched_at),
                    "http_status": a.http_status,
                    "acquisition_method": a.acquisition_method,
                    "checksum": a.checksum,
                }
                for a in artifacts
            ],
        }
        (self.program2_dir / "provenance.json").write_text(
            json.dumps(provenance, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def write_manifest(
        self,
        summary: RunSummary,
        artifacts: list[ArtifactRecord],
    ) -> Path:
        """Write the top-level manifest.json for the run."""
        categories = Counter(classify_content_type(a.content_type, a.source_url) for a in artifacts)
        manifest_data = {
            "contract_version": CONTRACT_VERSION,
            "run_id": summary.run_id,
            "site_id": self.site_id,
            "root_source_url": summary.root_source_url,
            "started_at": _iso(summary.started_at),
            "completed_at": _iso(summary.completed_at),
            "status": summary.status.value,
            "discovered": summary.discovered,
            "attempted": summary.attempted,
            "acquired": summary.acquired,
            "failed": summary.failed,
            "retries": summary.retries,
            "rate_limited": summary.rate_limited,
            "browser_pages": summary.browser_pages,
            "proxy_failures": summary.proxy_failures,
            "by_state": summary.by_state,
            "artifact_count": len(artifacts),
            "artifacts_by_category": dict(categories),
            "paths": {
                "run_dir": str(self.run_dir),
                "raw_dir": str(self.raw_dir),
                "evidence_dir": str(self.evidence_dir),
                "program2_dir": str(self.program2_dir),
            },
        }
        manifest_path = self.run_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return manifest_path
