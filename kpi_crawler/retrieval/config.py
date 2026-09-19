"""Configuration for the retrieval baseline, loaded from environment variables.

Kept separate from `kpi_crawler.config.Settings`: the retrieval baseline is an
independent, opt-in phase with its own required input (the KPI dictionary
path) that must not become a new requirement for the existing acquisition/
crawl/export commands.
"""

from dataclasses import dataclass
import os
from pathlib import Path

from ..config import ConfigurationError
from .embedding_client import DEFAULT_ENDPOINT, DEFAULT_MODEL


@dataclass(frozen=True)
class RetrievalSettings:
    database_url: str
    kpi_dictionary_path: Path
    embedding_model: str = DEFAULT_MODEL
    embedding_endpoint: str = DEFAULT_ENDPOINT
    embedding_dimension: int = 2560
    top_k: int = 10
    reports_dir: Path = Path("reports")

    @classmethod
    def from_environment(cls) -> "RetrievalSettings":
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise ConfigurationError("DATABASE_URL must be set")

        kpi_path_raw = os.environ.get("KPI_DICTIONARY_PATH", "").strip()
        if not kpi_path_raw:
            raise ConfigurationError(
                "KPI_DICTIONARY_PATH must be set to the real KPI dictionary xlsx file"
            )

        embedding_model = os.environ.get("EMBEDDING_MODEL", DEFAULT_MODEL).strip()
        if not embedding_model:
            raise ConfigurationError("EMBEDDING_MODEL must not be empty")

        embedding_endpoint = os.environ.get("EMBEDDING_ENDPOINT", DEFAULT_ENDPOINT).strip()
        if not embedding_endpoint:
            raise ConfigurationError("EMBEDDING_ENDPOINT must not be empty")

        dimension_raw = os.environ.get("EMBEDDING_DIMENSION", "2560")
        try:
            embedding_dimension = int(dimension_raw)
        except ValueError:
            raise ConfigurationError("EMBEDDING_DIMENSION must be an integer") from None
        if embedding_dimension <= 0:
            raise ConfigurationError("EMBEDDING_DIMENSION must be positive")

        top_k_raw = os.environ.get("RETRIEVAL_TOP_K", "10")
        try:
            top_k = int(top_k_raw)
        except ValueError:
            raise ConfigurationError("RETRIEVAL_TOP_K must be an integer") from None
        if top_k <= 0:
            raise ConfigurationError("RETRIEVAL_TOP_K must be positive")

        reports_dir = Path(os.environ.get("RETRIEVAL_REPORTS_DIR", "reports"))

        return cls(
            database_url=database_url,
            kpi_dictionary_path=Path(kpi_path_raw),
            embedding_model=embedding_model,
            embedding_endpoint=embedding_endpoint,
            embedding_dimension=embedding_dimension,
            top_k=top_k,
            reports_dir=reports_dir,
        )
