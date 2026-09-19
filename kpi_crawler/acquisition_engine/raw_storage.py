"""Content-addressed raw artifact storage.

Same convention as `kpi_crawler.acquisition`'s existing raw storage (SHA-256
filename under a storage directory, atomic write via a temp file + rename) so
raw bytes on disk stay compatible across both acquisition paths — but
implemented independently here since it is Program 1's own concern, not a
shared internal dependency on the older module.
"""

import os
from pathlib import Path
import tempfile

from ..errors import StorageError


def write_raw(content: bytes, storage_dir: Path, checksum: str) -> Path:
    path = storage_dir / f"{checksum}.raw"
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
        raise StorageError(f"failed to persist raw artifact {checksum}: {exc}") from exc
    return path
