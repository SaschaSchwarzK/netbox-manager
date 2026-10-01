"""
execute_job(): runs (or RESUMES) a MigrationJob to completion.

Resumability rests on three things:
1. Every MigrationJobItem/MigrationJobPatch was written IN FULL at plan time
   (registry.py + planner.py), in a fixed `order_index`. Nothing here ever
   re-plans or re-reads the source; execution only touches the target.
2. The id_map is rebuilt, at any moment, purely by querying rows with
   execution_status='done' — there is no separate map to fall out of sync.
3. Before actually creating an object, this module re-checks whether a
   matching object already exists on the target (the same natural-key
   matcher planning used, but now against the REAL, current id_map). This
   is what makes a resume safe even in the narrow window where a previous
   run's create() call succeeded on the target but the process crashed
   before committing that fact locally — the resumed run finds the object
   via natural key instead of creating a duplicate.

Commits after every single item/patch (not batched), so a crash loses at
most the one object in flight, never more. See app.main's startup hook for
how an orphaned "running" job (the owning process is gone — this is a
single-process deployment, so that's unambiguous) gets a resume thread
kicked off automatically.
"""
from __future__ import annotations

import json
import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MigrationJob, MigrationJobItem, MigrationJobPatch
from app.services.migration.client import MigrationApiError, RateLimitedClient
from app.services.migration.matcher import IdMap
from app.services.migration.planner import LiveTargetLookup, resolve_endpoint
from app.services.migration.registry import Registry, TypeSpec
from app.services.migration.sanitize import resolve_fk_refs, resolve_polymorphic_fk_refs
from app.services.syslog_client import send_audit_entry

HEARTBEAT_EVERY = 10  # items/patches between last_heartbeat_at updates — cheap enough to not throttle a large run


class JobCancelled(Exception):
    pass


def _load_id_map(db: Session, job_id: str) -> IdMap:
    id_map = IdMap()
    rows = db.execute(
        select(MigrationJobItem.object_type, MigrationJobItem.source_id, MigrationJobItem.target_id)
        .where(MigrationJobItem.job_id == job_id, MigrationJobItem.execution_status == "done", MigrationJobItem.target_id.is_not(None))
    ).all()
    for object_type, source_id, target_id in rows:
        id_map.put(object_type, source_id, target_id)
    return id_map


def _refresh_cancel_flag(db: Session, job_id: str) -> bool:
    return bool(db.execute(select(MigrationJob.cancel_requested).where(MigrationJob.id == job_id)).scalar_one())


def _check_cancelled(db: Session, job: MigrationJob, counter: int) -> None:
    if counter % HEARTBEAT_EVERY == 0:
        job.last_heartbeat_at = datetime.utcnow()
        db.commit()
        if _refresh_cancel_flag(db, job.id):
            raise JobCancelled()


def _execute_one_item(
    db: Session,
    item: MigrationJobItem,
    *,
    registry: Registry,
    target_client: RateLimitedClient,
    target_lookup: LiveTargetLookup,
    id_map: IdMap,
) -> None:
    type_spec = registry[item.object_type]
    static_fields = json.loads(item.payload_json)
    fk_refs = json.loads(item.fk_refs_json)

    resolved_fk, unresolved_fk = resolve_fk_refs(type_spec, fk_refs, id_map=id_map)
    if unresolved_fk:
        item.execution_status = "error"
        item.error_detail = f"Required dependency never resolved (skipped or ambiguous?): {sorted(unresolved_fk)}"
        item.executed_at = datetime.utcnow()
        db.commit()
        return

    payload = {**static_fields, **resolved_fk}
    endpoint = resolve_endpoint(target_client.nb, type_spec.endpoint)

    if item.planned_action == "create":
        # Safety net: a previous, crashed run may have already created this exact object on
        # the target without us having recorded it locally (the API call succeeded but the
        # process died before committing that fact). Re-check by natural key — using the
        # already-resolved, target-side field values we're about to send, not a fresh id_map
        # lookup (which only maps SOURCE ids, of no use once we're holding target ids) —
        # before creating what might be a duplicate.
        existing_target_id = _find_existing_by_natural_key(type_spec, static_fields, resolved_fk, target_lookup)
        if existing_target_id is not None:
            item.target_id = existing_target_id
            item.execution_status = "done"
            item.executed_at = datetime.utcnow()
            db.commit()
            id_map.put(item.object_type, item.source_id, item.target_id)
            return
        created = target_client.create(endpoint, payload)
        item.target_id = created.id if hasattr(created, "id") else created["id"]
    elif item.planned_action == "update":
        target_client.update_by_id(endpoint, item.target_id, payload)
        # target_id is already set (it was the matched object's real id since plan time).
    else:
        raise AssertionError(f"_execute_one_item called for non-executable action {item.planned_action!r}")

    item.execution_status = "done"
    item.executed_at = datetime.utcnow()
    db.commit()
    id_map.put(item.object_type, item.source_id, item.target_id)


def _find_existing_by_natural_key(
    type_spec: TypeSpec,
    static_fields: dict,
    resolved_fk: dict,
    target_lookup: LiveTargetLookup,
) -> int | None:
    """
    Directly queries the target for each of this type's match strategies,
    using values we already have fully resolved to the target's own ids
    (unlike matcher.match_object, which resolves FK fields from a SOURCE id
    through an id_map — not applicable here, since by this point we're
    holding target-side values, not source ids to look up). Strategies are
    tried in declared order; the first that finds something wins — this is
    a duplicate-prevention safety net for a single item, not the full
    plan-time cross-strategy ambiguity check (ambiguous items never reach
    execution at all; they're left in an error state at plan time).
    """
    for strategy in type_spec.match_strategies:
        filters: dict = {}
        matchable = True
        for field_name in strategy.fields:
            if field_name in strategy.fk_fields:
                value = resolved_fk.get(field_name)
                if value is None:
                    matchable = False
                    break
                filters[f"{field_name}_id"] = value
            else:
                value = static_fields.get(field_name)
                if value is None:
                    matchable = False
                    break
                filters[field_name] = value
        if not matchable:
            continue
        found = target_lookup.find(type_spec, filters)
        if found is not None:
            return found["id"]
    return None


def _execute_pending_items(
    db: Session,
    job: MigrationJob,
    *,
    registry: Registry,
    target_client: RateLimitedClient,
    id_map: IdMap,
) -> None:
    target_lookup = LiveTargetLookup(target_client)
    counter = 0
    while True:
        item = db.execute(
            select(MigrationJobItem)
            .where(
                MigrationJobItem.job_id == job.id,
                MigrationJobItem.execution_status == "pending",
            )
            .order_by(MigrationJobItem.order_index)
            .limit(1)
        ).scalar_one_or_none()
        if item is None:
            return
        counter += 1
        _check_cancelled(db, job, counter)
        try:
            _execute_one_item(db, item, registry=registry, target_client=target_client, target_lookup=target_lookup, id_map=id_map)
        except MigrationApiError as exc:
            item.execution_status = "error"
            item.error_detail = str(exc)
            item.executed_at = datetime.utcnow()
            db.commit()


def _execute_pending_patches(
    db: Session,
    job: MigrationJob,
    *,
    registry: Registry,
    target_client: RateLimitedClient,
    id_map: IdMap,
) -> None:
    counter = 0
    while True:
        patch = db.execute(
            select(MigrationJobPatch)
            .where(MigrationJobPatch.job_id == job.id, MigrationJobPatch.execution_status == "pending")
            .order_by(MigrationJobPatch.order_index)
            .limit(1)
        ).scalar_one_or_none()
        if patch is None:
            return
        counter += 1
        _check_cancelled(db, job, counter)

        type_spec = registry[patch.object_type]
        patch_fields = json.loads(patch.patch_fields_json)
        polymorphic_patch_fields = json.loads(patch.polymorphic_patch_fields_json or "{}")

        resolved, unresolved = resolve_fk_refs(type_spec, patch_fields, id_map=id_map)
        poly_resolved, poly_errors = resolve_polymorphic_fk_refs(polymorphic_patch_fields, id_map=id_map)

        all_errors = {**{k: f"unresolved: {v}" for k, v in unresolved.items()}, **poly_errors}
        if all_errors:
            patch.execution_status = "error"
            patch.error_detail = (
                f"Dependency never resolved (skipped or ambiguous?): {sorted(unresolved)}"
                if unresolved else
                f"Polymorphic FK could not be resolved: { {k: v for k, v in poly_errors.items()} }"
            )
            patch.executed_at = datetime.utcnow()
            db.commit()
            continue

        merged = {**resolved, **poly_resolved}

        target_id = id_map.get(patch.object_type, patch.source_id)
        if target_id is None or target_id < 0:
            # The object this patch belongs to was never actually created (its own item
            # errored out) — nothing to patch.
            patch.execution_status = "error"
            patch.error_detail = "The object this patch applies to was never created on the target."
            patch.executed_at = datetime.utcnow()
            db.commit()
            continue

        try:
            endpoint = resolve_endpoint(target_client.nb, type_spec.endpoint)
            target_client.update_by_id(endpoint, target_id, merged)
            patch.execution_status = "done"
        except MigrationApiError as exc:
            patch.execution_status = "error"
            patch.error_detail = str(exc)
        patch.executed_at = datetime.utcnow()
        db.commit()


def execute_job(
    db: Session,
    job: MigrationJob,
    *,
    registry: Registry,
    target_client: RateLimitedClient,
) -> None:
    """
    Runs `job` to completion from wherever it currently stands (fresh start
    if phase='primary' and nothing is 'done' yet; a genuine resume
    otherwise — the code path is identical either way). Raises nothing on
    a normal (even partially-failed) completion; only truly unexpected
    exceptions propagate, leaving the job row in `status='running'` for a
    future resume to pick up again (see app.main's startup hook).
    """
    if job.status not in ("planned", "running"):
        return  # already terminal — never re-execute a completed/failed/cancelled job by accident
    job.status = "running"
    if job.started_at is None:
        job.started_at = datetime.utcnow()
    db.commit()

    id_map = _load_id_map(db, job.id)

    try:
        if job.phase == "primary":
            _execute_pending_items(db, job, registry=registry, target_client=target_client, id_map=id_map)
            job.phase = "patch"
            db.commit()
        if job.phase == "patch":
            _execute_pending_patches(db, job, registry=registry, target_client=target_client, id_map=id_map)
            job.phase = "done"
            db.commit()
    except JobCancelled:
        job.status = "cancelled"
        job.finished_at = datetime.utcnow()
        db.commit()
        _send_completion_audit(job)
        return

    any_errors = db.execute(
        select(MigrationJobItem.id).where(MigrationJobItem.job_id == job.id, MigrationJobItem.execution_status == "error").limit(1)
    ).first() is not None or db.execute(
        select(MigrationJobPatch.id).where(MigrationJobPatch.job_id == job.id, MigrationJobPatch.execution_status == "error").limit(1)
    ).first() is not None

    job.status = "completed_with_errors" if any_errors else "completed"
    job.finished_at = datetime.utcnow()
    db.commit()
    _send_completion_audit(job)


def _send_completion_audit(job: MigrationJob) -> None:
    send_audit_entry({
        "action_type": "migration",
        "target_name": f"{job.source_instance_id} -> {job.target_instance_id}",
        "file_path": job.id,
        "status": "success" if job.status == "completed" else job.status,
        "detail": f"Migration job {job.id} finished with status={job.status}",
        "actor_name": job.actor_name,
        "actor_email": job.actor_email,
    })
