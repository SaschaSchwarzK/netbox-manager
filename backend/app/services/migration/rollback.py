"""Best-effort rollback for objects created by one migration job.

This is intentionally not a transactional undo. Only successful ``create``
items are deleted, in reverse planning order. Mapped and updated objects are
never modified because the migration does not retain their pre-update state.
Likewise, patches on surviving objects are left untouched. The marker tag
definition is retained because it is not a job item and may be shared; tag
associations on deleted objects disappear with those objects.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MigrationJob, MigrationJobItem
from app.services.migration.client import MigrationApiError, RateLimitedClient
from app.services.migration.planner import resolve_endpoint
from app.services.migration.registry import Registry
from app.services.syslog_client import send_audit_entry


def _refresh_rollback_totals(db: Session, job: MigrationJob) -> None:
    totals: dict[str, dict[str, int]] = {}
    rows = db.execute(
        select(MigrationJobItem.object_type, MigrationJobItem.execution_status)
        .where(MigrationJobItem.job_id == job.id, MigrationJobItem.planned_action == "create")
    ).all()
    for object_type, status in rows:
        counts = totals.setdefault(object_type, {})
        key = "rollback_error" if status == "rollback_error" else status
        counts[key] = counts.get(key, 0) + 1
    job.totals_json = json.dumps(totals)
    db.commit()


def rollback_job(
    db: Session,
    job: MigrationJob,
    *,
    registry: Registry,
    target_client: RateLimitedClient,
) -> None:
    """Delete this job's created objects, committing after every deletion."""
    job.status = "rolling_back"
    job.finished_at = None
    db.commit()

    while True:
        item = db.execute(
            select(MigrationJobItem)
            .where(
                MigrationJobItem.job_id == job.id,
                MigrationJobItem.planned_action == "create",
                MigrationJobItem.execution_status == "done",
                MigrationJobItem.target_id.is_not(None),
            )
            .order_by(MigrationJobItem.order_index.desc())
            .limit(1)
        ).scalar_one_or_none()
        if item is None:
            break

        endpoint = resolve_endpoint(target_client.nb, registry[item.object_type].endpoint)
        try:
            target_client.delete_by_id(endpoint, item.target_id)
            item.execution_status = "rolled_back"
            item.error_detail = None
        except MigrationApiError as exc:
            item.execution_status = "rollback_error"
            item.error_detail = f"Rollback delete failed: {exc}"
        item.executed_at = datetime.utcnow()
        db.commit()
        _refresh_rollback_totals(db, job)

    has_errors = db.execute(
        select(MigrationJobItem.id).where(
            MigrationJobItem.job_id == job.id,
            MigrationJobItem.execution_status == "rollback_error",
        ).limit(1)
    ).first() is not None
    _refresh_rollback_totals(db, job)
    job.status = "rolled_back_with_errors" if has_errors else "rolled_back"
    job.finished_at = datetime.utcnow()
    db.commit()
    send_audit_entry({
        "action_type": "migration_rollback",
        "target_name": f"{job.source_instance_id} -> {job.target_instance_id}",
        "file_path": job.id,
        "status": job.status,
        "detail": f"Migration job {job.id} rollback finished with status={job.status}",
        "actor_name": job.actor_name,
        "actor_email": job.actor_email,
    })
