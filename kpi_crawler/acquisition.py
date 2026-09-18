"""Generic source acquisition and raw content storage."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, url2pathname, urlopen

from . import __version__
from .errors import ApplicationError, OperationalLimitError, StorageError


RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_RETRIES = 2
DEFAULT_MAX_ARTIFACT_BYTES = 50_000_000
USER_AGENT = f"kpi-crawler/{__version__}"


class AcquisitionError(ApplicationError):
    """Raised when a source cannot be acquired."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class AcquiredSource:
    source_url: str
    resolved_url: str
    source_type: str
    content_type: str
    status_code: int | None
    content: bytes
    sha256: str
    raw_storage_ref: str
    retrieved_at: datetime


def _content_type(content: bytes, header_content_type: str | None) -> str:
    if header_content_type and header_content_type != "application/octet-stream":
        return header_content_type
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    return "application/octet-stream"


def _write_raw(content: bytes, storage_dir: Path, sha256: str) -> Path:
    path = storage_dir / f"{sha256}.raw"
    temporary_path: Path | None = None
    try:
        storage_dir.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return path
        with tempfile.NamedTemporaryFile(dir=storage_dir, delete=False) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise StorageError(f"failed to persist raw artifact {sha256}: {exc}") from exc
    return path


def _read_bounded(response, max_bytes: int, source_url: str) -> bytes:
    declared_size = response.headers.get("Content-Length")
    if declared_size is not None:
        try:
            if int(declared_size) > max_bytes:
                raise OperationalLimitError(
                    f"{source_url}: declared content length {declared_size} exceeds the {max_bytes}-byte limit"
                )
        except ValueError:
            pass
    content = response.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise OperationalLimitError(
            f"{source_url}: content exceeds the {max_bytes}-byte limit"
        )
    return content


def _read_http(
    source_url: str, timeout_seconds: float, retries: int, max_bytes: int
) -> tuple[bytes, str, int, str]:
    for attempt in range(retries + 1):
        try:
            request = Request(source_url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=timeout_seconds) as response:
                return (
                    _read_bounded(response, max_bytes, source_url),
                    response.headers.get_content_type(),
                    response.status,
                    response.geturl(),
                )
        except HTTPError as exc:
            try:
                if exc.code not in RETRYABLE_HTTP_STATUSES or attempt == retries:
                    raise AcquisitionError(f"HTTP {exc.code} acquiring {source_url}", exc.code) from exc
            finally:
                exc.close()
        except (TimeoutError, URLError) as exc:
            if attempt == retries:
                raise AcquisitionError(f"failed to acquire {source_url}: {exc}") from exc
        time.sleep(0.1 * (attempt + 1))
    raise AssertionError("unreachable")


def acquire_source(
    source_url: str,
    storage_dir: Path,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> AcquiredSource:
    """Fetch an HTTP(S) URL or local file and persist its raw bytes by SHA-256."""
    parsed = urlsplit(source_url)
    retrieved_at = datetime.now(timezone.utc)
    if parsed.scheme in {"http", "https"}:
        content, header_content_type, status_code, resolved_url = _read_http(
            source_url, timeout_seconds, retries, max_bytes
        )
        source_type = "http"
    elif parsed.scheme == "file" or not parsed.scheme:
        path = Path(url2pathname(parsed.path) if parsed.scheme == "file" else source_url)
        try:
            file_size = path.stat().st_size
        except OSError as exc:
            raise AcquisitionError(f"failed to read {source_url}: {exc}") from exc
        if file_size > max_bytes:
            raise OperationalLimitError(
                f"{source_url}: file size {file_size} exceeds the {max_bytes}-byte limit"
            )
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise AcquisitionError(f"failed to read {source_url}: {exc}") from exc
        header_content_type = None
        status_code = None
        resolved_url = source_url
        source_type = "file"
    else:
        raise AcquisitionError(f"unsupported source scheme: {parsed.scheme}")

    sha256 = hashlib.sha256(content).hexdigest()
    raw_path = _write_raw(content, storage_dir, sha256)
    return AcquiredSource(
        source_url=source_url,
        resolved_url=resolved_url,
        source_type=source_type,
        content_type=_content_type(content, header_content_type),
        status_code=status_code,
        content=content,
        sha256=sha256,
        raw_storage_ref=str(raw_path),
        retrieved_at=retrieved_at,
    )
