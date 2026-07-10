from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


def repository_setup_lock_path(data_dir: Path, repo_path: Path) -> Path:
    canonical = str(Path(repo_path).resolve())
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Path(data_dir) / "locks" / "repository-setup" / f"{digest}.lock"


@contextmanager
def repository_setup_lock(data_dir: Path, repo_path: Path) -> Iterator[Path]:
    lock_path = repository_setup_lock_path(data_dir, repo_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield lock_path
    finally:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
