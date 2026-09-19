"""Application-level errors that can be reported without a traceback."""


class ApplicationError(Exception):
    """Base class for expected application failures."""


class DatabaseError(ApplicationError):
    """Raised when PostgreSQL cannot be reached or used safely."""


class StorageError(ApplicationError):
    """Raised when acquired content cannot be durably persisted to raw storage."""


class ExtractionError(ApplicationError):
    """Raised when acquired content cannot be parsed into evidence."""


class UnsupportedContentError(ApplicationError):
    """Raised when acquired content's type is not supported by any extractor."""


class OperationalLimitError(ApplicationError):
    """Raised when a configured bounded-resource limit is reached."""


class ExportError(ApplicationError):
    """Raised when a run cannot be exported."""


class KPIDictionaryError(ApplicationError):
    """Raised when the configured KPI dictionary file is missing or malformed."""


class EmbeddingError(ApplicationError):
    """Raised when the local embedding runtime cannot be reached or fails."""
