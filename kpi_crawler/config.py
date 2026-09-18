"""Application configuration loaded from environment variables."""

from dataclasses import dataclass
import os
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when required application configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    log_level: str = "INFO"
    artifact_storage_dir: Path = Path(".data/artifacts")
    export_dir: Path = Path("exports")
    acquisition_timeout_seconds: float = 10.0
    acquisition_retries: int = 2
    max_artifact_bytes: int = 50_000_000
    allowed_content_types: frozenset[str] = frozenset({"application/pdf"})
    crawl_max_depth: int = 2
    crawl_max_artifacts: int = 50

    @classmethod
    def from_environment(cls) -> "Settings":
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise ConfigurationError("DATABASE_URL must be set")

        log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL must be a standard Python log level")
        storage_dir = Path(os.environ.get("ARTIFACT_STORAGE_DIR", ".data/artifacts"))
        export_dir = Path(os.environ.get("EXPORT_DIR", "exports"))

        timeout_raw = os.environ.get("ACQUISITION_TIMEOUT_SECONDS", "10")
        try:
            timeout_seconds = float(timeout_raw)
        except ValueError:
            raise ConfigurationError("ACQUISITION_TIMEOUT_SECONDS must be a number") from None
        if timeout_seconds <= 0:
            raise ConfigurationError("ACQUISITION_TIMEOUT_SECONDS must be positive")

        retries_raw = os.environ.get("ACQUISITION_RETRIES", "2")
        try:
            retries = int(retries_raw)
        except ValueError:
            raise ConfigurationError("ACQUISITION_RETRIES must be an integer") from None
        if retries < 0:
            raise ConfigurationError("ACQUISITION_RETRIES must not be negative")

        max_bytes_raw = os.environ.get("MAX_ARTIFACT_BYTES", "50000000")
        try:
            max_artifact_bytes = int(max_bytes_raw)
        except ValueError:
            raise ConfigurationError("MAX_ARTIFACT_BYTES must be an integer") from None
        if max_artifact_bytes <= 0:
            raise ConfigurationError("MAX_ARTIFACT_BYTES must be positive")

        # Content types are always lowercase elsewhere in the system (Python's
        # email.message.Message.get_content_type(), used for HTTP responses, already
        # normalizes to lowercase), so an override is normalized the same way here —
        # otherwise an operator-set "Application/PDF" would silently never match.
        allowed_types_raw = os.environ.get("ALLOWED_CONTENT_TYPES", "application/pdf")
        allowed_content_types = frozenset(
            item.strip().lower() for item in allowed_types_raw.split(",") if item.strip()
        )
        if not allowed_content_types:
            raise ConfigurationError("ALLOWED_CONTENT_TYPES must not be empty")

        depth_raw = os.environ.get("CRAWL_MAX_DEPTH", "2")
        try:
            crawl_max_depth = int(depth_raw)
        except ValueError:
            raise ConfigurationError("CRAWL_MAX_DEPTH must be an integer") from None
        if crawl_max_depth < 0:
            raise ConfigurationError("CRAWL_MAX_DEPTH must not be negative")

        crawl_artifacts_raw = os.environ.get("CRAWL_MAX_ARTIFACTS", "50")
        try:
            crawl_max_artifacts = int(crawl_artifacts_raw)
        except ValueError:
            raise ConfigurationError("CRAWL_MAX_ARTIFACTS must be an integer") from None
        if crawl_max_artifacts <= 0:
            raise ConfigurationError("CRAWL_MAX_ARTIFACTS must be positive")

        return cls(
            database_url=database_url,
            log_level=log_level,
            artifact_storage_dir=storage_dir,
            export_dir=export_dir,
            acquisition_timeout_seconds=timeout_seconds,
            acquisition_retries=retries,
            max_artifact_bytes=max_artifact_bytes,
            allowed_content_types=allowed_content_types,
            crawl_max_depth=crawl_max_depth,
            crawl_max_artifacts=crawl_max_artifacts,
        )
