"""Command-line entry point."""

import argparse
import logging
import sys

from .acquisition import AcquisitionError
from .config import ConfigurationError, Settings
from .crawl import run_crawl
from .errors import (
    ApplicationError,
    DatabaseError,
    ExtractionError,
    OperationalLimitError,
    StorageError,
    UnsupportedContentError,
)
from .export import export_run
from .logging import configure_logging
from .migrations import migrate
from .pipeline import run_pdf_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kpi-crawler")
    parser.add_argument(
        "command", choices=("start", "migrate", "process", "crawl", "export"), nargs="?", default="start"
    )
    parser.add_argument("source_url", nargs="?")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        settings = Settings.from_environment()
        configure_logging(settings.log_level)
        args = build_parser().parse_args(argv)
        command = args.command
        if command == "migrate":
            count = migrate(settings.database_url)
            logging.getLogger(__name__).info("migrations complete: %d applied", count)
        elif command == "process":
            if not args.source_url:
                raise ValueError("process requires a source URL")
            result = run_pdf_pipeline(
                args.source_url,
                settings.database_url,
                settings.artifact_storage_dir,
                timeout_seconds=settings.acquisition_timeout_seconds,
                retries=settings.acquisition_retries,
                max_artifact_bytes=settings.max_artifact_bytes,
                allowed_content_types=settings.allowed_content_types,
            )
            logging.getLogger(__name__).info(
                "discovered=1 acquired=1 rejected=0 processed artifact %s with %d evidence records",
                result.artifact.sha256,
                len(result.evidence),
            )
        elif command == "crawl":
            if not args.source_url:
                raise ValueError("crawl requires a source URL")
            crawl_result = run_crawl(
                args.source_url,
                settings.database_url,
                settings.artifact_storage_dir,
                max_depth=settings.crawl_max_depth,
                max_artifacts=settings.crawl_max_artifacts,
                timeout_seconds=settings.acquisition_timeout_seconds,
                retries=settings.acquisition_retries,
                max_artifact_bytes=settings.max_artifact_bytes,
                allowed_content_types=settings.allowed_content_types,
            )
            logging.getLogger(__name__).info(
                "discovered=%d acquired=%d rejected=%d crawl run %s complete",
                crawl_result.discovered,
                crawl_result.acquired,
                crawl_result.rejected,
                crawl_result.run_id,
            )
        elif command == "export":
            if not args.source_url:
                raise ValueError("export requires a run ID")
            try:
                run_id = int(args.source_url)
            except ValueError:
                raise ValueError(f"invalid run ID: {args.source_url!r}") from None
            export_result = export_run(run_id, settings.database_url, settings.export_dir)
            logging.getLogger(__name__).info(
                "exported run %d to %s (%d sources, %d evidence records, %d rejected)",
                export_result.run_id,
                export_result.output_dir,
                export_result.source_count,
                export_result.evidence_count,
                export_result.rejected_count,
            )
        else:
            logging.getLogger(__name__).info("application started")
        return 0
    except UnsupportedContentError as exc:
        logging.getLogger(__name__).error("discovered=1 acquired=1 rejected=1 unsupported content: %s", exc)
        return 1
    except OperationalLimitError as exc:
        logging.getLogger(__name__).error("discovered=1 acquired=0 rejected=1 limit exceeded: %s", exc)
        return 1
    except AcquisitionError as exc:
        logging.getLogger(__name__).error("discovered=1 acquired=0 rejected=0 acquisition failed: %s", exc)
        return 1
    except ExtractionError as exc:
        logging.getLogger(__name__).error("discovered=1 acquired=1 rejected=0 extraction failed: %s", exc)
        return 1
    except (StorageError, DatabaseError) as exc:
        logging.getLogger(__name__).error("storage failed: %s", exc)
        return 1
    except (ApplicationError, ConfigurationError, ValueError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
