import logging
from datetime import timedelta
from pathlib import Path

from app import models
from app.config import settings
from app.database import SessionLocal
from app.services.export import storage
from app.timeutil import utcnow

logger = logging.getLogger(__name__)
FINISHED = {"completed", "failed", "cancelled", "expired"}


def recover_after_restart() -> int:
    db = SessionLocal()
    try:
        now = utcnow()
        changed = db.query(models.ExportJob).filter(models.ExportJob.status.in_({"queued", "running"})).update({
            models.ExportJob.status: "failed", models.ExportJob.error: "Interrupted by restart",
            models.ExportJob.finished_at: now,
            models.ExportJob.expires_at: now + timedelta(days=settings.export_retention_days),
        }, synchronize_session=False)
        db.commit()
        storage.sweep_tmp()
        return changed
    finally:
        db.close()


def run_cleanup() -> None:
    db = SessionLocal()
    deleted_files = deleted_rows = orphans = 0
    try:
        now = utcnow()
        expired = db.query(models.ExportJob).filter(
            models.ExportJob.expires_at < now, models.ExportJob.status.in_(FINISHED - {"expired"})
        ).all()
        for job in expired:
            deleted_files += storage.delete_job_files(job)
            job.status, job.file_name = "expired", None
        cutoff = now - timedelta(hours=24)
        old = db.query(models.ExportJob).filter(models.ExportJob.expires_at < cutoff).all()
        for job in old:
            storage.delete_job_files(job)
            db.delete(job)
            deleted_rows += 1
        known = {name for (name,) in db.query(models.ExportJob.file_name).filter(models.ExportJob.file_name.is_not(None)).all()}
        files_dir = Path(settings.export_dir) / "files"
        orphan_cutoff = (now - timedelta(hours=1)).timestamp()
        if files_dir.exists():
            for path in files_dir.iterdir():
                if path.is_file() and path.name not in known and path.stat().st_mtime < orphan_cutoff:
                    path.unlink()
                    orphans += 1
        tmp = storage.sweep_tmp(now - timedelta(hours=24))
        db.commit()
        logger.info("Export cleanup: files=%d rows=%d orphans=%d tmp=%d", deleted_files, deleted_rows, orphans, tmp)
    finally:
        db.close()
