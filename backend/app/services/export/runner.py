from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from app import crypto, models
from app.config import settings
from app.database import SessionLocal
from app.services import netbox_client
from app.services.export import engine
from app.services.migration.client import build_client
from app.services.syslog_client import send_audit_entry
from app.timeutil import utcnow

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=settings.export_max_concurrent_jobs, thread_name_prefix="export")


def submit_job(job_id: str) -> None:
    _executor.submit(_run, job_id)


def shutdown() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)


def _terminal(job, status: str, error: str | None = None) -> None:
    job.status = status
    job.error = error[:512] if error else None
    job.finished_at = utcnow()
    job.expires_at = job.finished_at + timedelta(days=settings.export_retention_days)


def _run(job_id: str) -> None:
    db = SessionLocal()
    try:
        changed = db.query(models.ExportJob).filter(
            models.ExportJob.id == job_id, models.ExportJob.status == "queued"
        ).update({models.ExportJob.status: "running", models.ExportJob.started_at: utcnow()}, synchronize_session=False)
        db.commit()
        if changed != 1:
            return
        job = db.get(models.ExportJob, job_id)
        logger.info("Export job started: %s", job_id)
        instance = db.get(models.NetboxInstance, job.instance_id) if job.instance_id else None
        if not instance:
            _terminal(job, "failed", "Instance no longer exists")
            db.commit()
            send_audit_entry({
                "action_type": "export", "target_name": f"{job.instance_name} / {job.tenant_name}",
                "file_path": job.id, "status": "error",
                "detail": f"Export job {job.id} failed because the instance no longer exists",
                "actor_name": job.actor_name, "actor_email": job.actor_email,
            })
            return
        client = build_client(
            instance.base_url, crypto.decrypt(instance.api_token_encrypted),
            netbox_client.verify_for_instance(instance), read_only=True,
            max_requests_per_second=settings.export_max_requests_per_second,
        )
        last_progress = 0.0
        progress_state = json.loads(job.progress_json or "{}")

        def progress(type_key: str, done: int, total: int) -> None:
            nonlocal last_progress
            progress_state[type_key] = {"done": done, "total": total}
            now = time.monotonic()
            if now - last_progress >= 2:
                db.query(models.ExportJob).filter_by(id=job_id).update({"progress_json": json.dumps(progress_state)})
                db.commit()
                last_progress = now

        last_cancel_check, cancelled = 0.0, False

        def is_cancelled() -> bool:
            nonlocal last_cancel_check, cancelled
            now = time.monotonic()
            if now - last_cancel_check >= 2:
                cancelled = bool(db.query(models.ExportJob.cancel_requested).filter_by(id=job_id).scalar())
                last_cancel_check = now
            return cancelled

        spec = engine.JobSpec(job.id, job.tenant_id, json.loads(job.object_types_json),
                              json.loads(job.fields_json), job.format, job.delimiter)
        try:
            result = engine.run_export(spec, client, progress, is_cancelled)
            db.refresh(job)
            job.file_name, job.file_size = result.file_name, result.file_size
            job.row_counts_json = json.dumps(result.row_counts)
            job.progress_json = json.dumps(progress_state)
            _terminal(job, "completed")
            audit_status = "success"
            logger.info("Export job completed: %s rows=%s bytes=%s", job.id, result.row_counts, result.file_size)
        except engine.ExportCancelled:
            db.refresh(job)
            _terminal(job, "cancelled")
            audit_status = "cancelled"
            logger.info("Export job cancelled: %s", job.id)
        except Exception as exc:
            db.refresh(job)
            _terminal(job, "failed", str(exc) or "Export failed")
            audit_status = "error"
            logger.exception("Export job failed: %s", job.id)
        db.commit()
        send_audit_entry({
            "action_type": "export", "target_name": f"{job.instance_name} / {job.tenant_name}",
            "file_path": job.id, "status": audit_status,
            "detail": f"Export job {job.id} finished with status={job.status}; rows={job.row_counts_json}",
            "actor_name": job.actor_name, "actor_email": job.actor_email,
        })
    finally:
        db.close()
