"""Content-addressed raw artifact storage helper.

Maintained for backwards compatibility; run-scoped acquisition engine runs
delegate to `RunStorage` in `kpi_crawler.acquisition_engine.storage`.
"""

import os
from pathlib import Path
import tempfile

from ..errors import StorageError


def write_raw(content: bytes, storage_dir: Path, checksum: str) -> Path:
    storage_dir = Path(storage_dir).resolve()
    path = (storage_dir / f"{checksum}.raw").resolve()

    # Path containment check
    try:
        path.relative_to(storage_dir)
    except ValueError as exc:
        raise StorageError(f"security violation: artifact path escapes storage directory: {exc}") from exc

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
