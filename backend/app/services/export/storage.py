import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
EXTENSIONS = {"csv", "xlsx", "zip"}
available = False


def _root() -> Path:
    return Path(settings.export_dir).resolve()


def ensure_dirs() -> bool:
    global available
    try:
        root = _root()
        for path in (root, root / "files", root / "tmp"):
            path.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-test"
        probe.write_bytes(b"ok")
        probe.unlink()
        available = True
    except OSError:
        available = False
        logger.error("Export storage is not writable at %s", settings.export_dir)
    return available


def _safe_path(directory: str, job_id: str, ext: str, suffix: str = "") -> Path:
    if not UUID_RE.fullmatch(job_id) or ext not in EXTENSIONS:
        raise ValueError("Invalid export file identifier or extension.")
    root = _root()
    candidate = (root / directory / f"{job_id}{suffix}.{ext}").resolve()
    if root not in candidate.parents:
        raise ValueError("Export path escapes the configured directory.")
    return candidate


def final_path(job_id: str, ext: str) -> Path:
    return _safe_path("files", job_id, ext)


def tmp_path(job_id: str, ext: str, suffix: str = "") -> Path:
    if suffix and not re.fullmatch(r"-[A-Za-z0-9_-]+", suffix):
        raise ValueError("Invalid temporary export suffix.")
    return _safe_path("tmp", job_id, ext, suffix)


def free_mib() -> int:
    return shutil.disk_usage(_root()).free // (1024 * 1024)


def delete_job_files(job) -> int:
    deleted = 0
    for ext in EXTENSIONS:
        path = final_path(job.id, ext)
        if path.exists():
            path.unlink()
            deleted += 1
    return deleted


def sweep_tmp(older_than: datetime | None = None) -> int:
    deleted = 0
    directory = _root() / "tmp"
    if not directory.exists():
        return 0
    cutoff = older_than.timestamp() if older_than else None
    for path in directory.iterdir():
        if path.is_file() and (cutoff is None or path.stat().st_mtime < cutoff):
            path.unlink()
            deleted += 1
    return deleted
