"""Standalone CLI entry point for the retrieval baseline: `kpi-crawler-retrieval`.

Kept separate from `kpi_crawler.cli` so the existing acquisition/crawl/export
command surface and its required configuration are completely unaffected.
"""

import json
import logging
import sys

from ..config import ConfigurationError
from ..db import connection
from ..errors import ApplicationError
from ..logging import configure_logging
from .config import RetrievalSettings
from .pipeline import run_baseline
from .report import write_report
from .store import prepare_connection


def main(argv: list[str] | None = None) -> int:
    try:
        settings = RetrievalSettings.from_environment()
        configure_logging("INFO")
        logger = logging.getLogger(__name__)

        logger.info(
            "retrieval baseline starting: kpi_dictionary=%s model=%s top_k=%d",
            settings.kpi_dictionary_path,
            settings.embedding_model,
            settings.top_k,
        )
        outcome = run_baseline(settings)

        with connection(settings.database_url) as conn:
            prepare_connection(conn)
            report_path = write_report(conn, outcome, settings)

        reproducibility = {
            "run_id": outcome.metrics.run_id,
            "started_at": outcome.metrics.started_at.isoformat(),
            "finished_at": outcome.metrics.finished_at.isoformat() if outcome.metrics.finished_at else None,
            "model_identifier": outcome.metrics.embedding_config.model_identifier,
            "embedding_dimension": outcome.metrics.embedding_config.expected_dimension,
            "embedding_runtime": "ollama-local",
            "embedding_endpoint": outcome.metrics.embedding_config.endpoint,
            "normalized": outcome.metrics.embedding_config.normalize,
            "instruction": outcome.metrics.embedding_config.instruction,
            "kpi_source_file": outcome.metrics.kpi_source_file,
            "kpi_count": outcome.metrics.kpi_count,
            "evidence_count": outcome.metrics.evidence_count,
            "evidence_by_type": outcome.metrics.evidence_by_type,
            "retrieval_k": outcome.metrics.top_k,
            "similarity_metric": "cosine",
            "representation_a": "evidence.canonical_text",
            "representation_b": "evidence.supporting_context",
        }
        (outcome.run_dir / "reproducibility.json").write_text(
            json.dumps(reproducibility, indent=2), encoding="utf-8"
        )

        logger.info(
            "retrieval baseline complete: run_dir=%s kpis=%d evidence=%d report=%s",
            outcome.run_dir,
            outcome.metrics.kpi_count,
            outcome.metrics.evidence_count,
            report_path,
        )
        return 0
    except (ApplicationError, ConfigurationError, ValueError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
