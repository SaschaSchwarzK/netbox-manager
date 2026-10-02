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
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MigrationJob, MigrationJobItem, MigrationJobPatch
from app.services.migration.client import MigrationApiError, RateLimitedClient
from app.services.migration.matcher import IdMap
from app.services.migration.planner import LiveTargetLookup, resolve_endpoint
from app.services.migration.registry import UNIVERSAL_TAG_FIELD, UNIVERSAL_TAG_TYPE, Registry, TypeSpec
from app.services.migration.sanitize import resolve_fk_refs, resolve_polymorphic_fk_refs
from app.services.syslog_client import send_audit_entry

HEARTBEAT_EVERY = 10  # items/patches between last_heartbeat_at updates — cheap enough to not throttle a large run
BULK_CREATE_SIZE = 100


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


def _refresh_live_totals(db: Session, job: MigrationJob) -> None:
    """Replace plan-time predictions with execution counts derived from durable rows."""
    totals: dict[str, dict[str, int]] = {}
    errored_patches = set(db.execute(
        select(MigrationJobPatch.object_type, MigrationJobPatch.source_id)
        .where(MigrationJobPatch.job_id == job.id, MigrationJobPatch.execution_status == "error")
    ).all())
    items = db.execute(
        select(
            MigrationJobItem.object_type, MigrationJobItem.source_id,
            MigrationJobItem.planned_action, MigrationJobItem.execution_status,
        )
        .where(MigrationJobItem.job_id == job.id)
    ).all()
    for object_type, source_id, planned_action, execution_status in items:
        counts = totals.setdefault(object_type, {})
        if execution_status == "error" or (object_type, source_id) in errored_patches:
            key = "error"
        elif execution_status == "done":
            key = planned_action
        else:
            continue
        counts[key] = counts.get(key, 0) + 1
    job.totals_json = json.dumps(totals)
    db.commit()


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
        # target_id is already set (it was the matched object's real id since plan time) —
        # restored after the bulk-create refactor accidentally dropped this branch, which
        # left every conflict_policy="update" item hitting the else below and crashing the
        # whole job with an uncaught AssertionError.
        target_client.update_by_id(endpoint, item.target_id, payload)
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


def _mark_done(db: Session, item: MigrationJobItem, target_id: int, id_map: IdMap) -> None:
    item.target_id = target_id
    item.execution_status = "done"
    item.executed_at = datetime.utcnow()
    db.commit()
    id_map.put(item.object_type, item.source_id, target_id)


def _execute_create_batch(
    db: Session,
    items: list[MigrationJobItem],
    *,
    registry: Registry,
    target_client: RateLimitedClient,
    target_lookup: LiveTargetLookup,
    id_map: IdMap,
    fail_fast: bool,
) -> bool:
    """Create independent adjacent items in one POST; return whether fail-fast stopped."""
    prepared: list[tuple[MigrationJobItem, dict[str, Any], dict[str, Any], TypeSpec]] = []
    for item in items:
        type_spec = registry[item.object_type]
        static_fields = json.loads(item.payload_json)
        fk_refs = json.loads(item.fk_refs_json)
        resolved_fk, unresolved_fk = resolve_fk_refs(type_spec, fk_refs, id_map=id_map)
        if unresolved_fk:
            item.execution_status = "error"
            item.error_detail = f"Required dependency never resolved (skipped or ambiguous?): {sorted(unresolved_fk)}"
            item.executed_at = datetime.utcnow()
            db.commit()
            if fail_fast:
                return True
            continue
        existing_id = _find_existing_by_natural_key(type_spec, static_fields, resolved_fk, target_lookup)
        if existing_id is not None:
            _mark_done(db, item, existing_id, id_map)
            continue
        prepared.append((item, {**static_fields, **resolved_fk}, {**static_fields, **resolved_fk}, type_spec))

    if not prepared:
        return False
    endpoint = resolve_endpoint(target_client.nb, prepared[0][3].endpoint)
    try:
        created_records = target_client.create_many(endpoint, [payload for _, payload, _, _ in prepared])
    except MigrationApiError as exc:
        target_lookup.clear_cache()
        for item, payload, _, type_spec in prepared:
            static_fields = json.loads(item.payload_json)
            resolved_fk, _ = resolve_fk_refs(type_spec, json.loads(item.fk_refs_json), id_map=id_map)
            existing_id = _find_existing_by_natural_key(type_spec, static_fields, resolved_fk, target_lookup)
            if existing_id is not None:
                _mark_done(db, item, existing_id, id_map)
            else:
                item.execution_status = "error"
                item.error_detail = str(exc)
                item.executed_at = datetime.utcnow()
                db.commit()
                if fail_fast:
                    break
        return fail_fast

    for (item, payload, _, _), created in zip(prepared, created_records):
        target_id = created.id if hasattr(created, "id") else created.get("id")
        if target_id is None:
            item.execution_status = "error"
            item.error_detail = "Bulk create response did not contain an id."
            item.executed_at = datetime.utcnow()
            db.commit()
            if fail_fast:
                return True
            continue
        _mark_done(db, item, target_id, id_map)
    return False


def _execute_pending_items(
    db: Session,
    job: MigrationJob,
    *,
    registry: Registry,
    target_client: RateLimitedClient,
    id_map: IdMap,
    fail_fast: bool,
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
            _refresh_live_totals(db, job)
            return
        counter += 1
        _check_cancelled(db, job, counter)
        if item.planned_action == "create":
            batch = db.execute(
                select(MigrationJobItem)
                .where(
                    MigrationJobItem.job_id == job.id,
                    MigrationJobItem.execution_status == "pending",
                    MigrationJobItem.object_type == item.object_type,
                    MigrationJobItem.order_index >= item.order_index,
                )
                .order_by(MigrationJobItem.order_index)
                .limit(BULK_CREATE_SIZE)
            ).scalars().all()
            contiguous: list[MigrationJobItem] = []
            for candidate in batch:
                if candidate.planned_action != "create":
                    break
                contiguous.append(candidate)
            if _execute_create_batch(
                db, contiguous, registry=registry, target_client=target_client,
                target_lookup=target_lookup, id_map=id_map, fail_fast=fail_fast,
            ):
                _refresh_live_totals(db, job)
                return
            _refresh_live_totals(db, job)
            continue
        try:
            _execute_one_item(db, item, registry=registry, target_client=target_client, target_lookup=target_lookup, id_map=id_map)
        except MigrationApiError as exc:
            item.execution_status = "error"
            item.error_detail = str(exc)
            item.executed_at = datetime.utcnow()
            db.commit()
            if fail_fast:
                _refresh_live_totals(db, job)
                return
        _refresh_live_totals(db, job)


def _execute_pending_patches(
    db: Session,
    job: MigrationJob,
    *,
    registry: Registry,
    target_client: RateLimitedClient,
    id_map: IdMap,
    fail_fast: bool,
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
            _refresh_live_totals(db, job)
            return
        counter += 1
        _check_cancelled(db, job, counter)

        type_spec = registry[patch.object_type]
        patch_fields = json.loads(patch.patch_fields_json)
        polymorphic_patch_fields = json.loads(patch.polymorphic_patch_fields_json or "{}")

        resolved, unresolved = resolve_fk_refs(type_spec, patch_fields, id_map=id_map)
        poly_resolved, poly_errors = resolve_polymorphic_fk_refs(polymorphic_patch_fields, id_map=id_map)

        merged = {**resolved, **poly_resolved}
        all_errors = {**{k: f"unresolved: {v}" for k, v in unresolved.items()}, **poly_errors}

        target_id = id_map.get(patch.object_type, patch.source_id)
        if target_id is None or target_id < 0:
            # The object this patch belongs to was never actually created (its own item
            # errored out) — nothing to patch.
            patch.execution_status = "error"
            patch.error_detail = "The object this patch applies to was never created on the target."
            patch.executed_at = datetime.utcnow()
            db.commit()
            _refresh_live_totals(db, job)
            continue

        try:
            endpoint = resolve_endpoint(target_client.nb, type_spec.endpoint)
            if merged:
                target_client.update_by_id(endpoint, target_id, merged)
            if all_errors:
                patch.execution_status = "error"
                patch.error_detail = f"Some deferred fields could not be resolved: {all_errors}"
            else:
                patch.execution_status = "done"
        except MigrationApiError as exc:
            patch.execution_status = "error"
            patch.error_detail = str(exc)
        patch.executed_at = datetime.utcnow()
        db.commit()
        _refresh_live_totals(db, job)
        if fail_fast and patch.execution_status == "error":
            return


def _apply_marker_tag(
    db: Session,
    job: MigrationJob,
    *,
    registry: Registry,
    target_client: RateLimitedClient,
    id_map: IdMap,
    tag_slug: str,
) -> None:
    """
    Tags every object actually CREATED by this job (never a mapped one — the
    same "never modify a mapped object" rule conflict policy already
    follows) with a marker tag, so a human can find and bulk-manage migrated
    objects later. Reuses the same extras.tag type the universal `tags`
    field resolution (see sanitize.py / registry.py's UNIVERSAL_TAG_*) is
    built on, rather than a separate one-off mechanism.

    Not tracked per-object the way phase `primary`/`patch` execution is —
    there is no persisted row recording "this object has been marker-
    tagged". If interrupted mid-way, re-running this step is SAFE (it
    recomputes each object's full, exact tag list every time, so re-applying
    is a no-op in effect) but not incremental: a resumed job re-examines
    every created object again rather than picking up only the untagged
    remainder. Accepted as a reasonable trade-off given how cheap and side-
    effect-free re-checking an already-tagged object is, versus the added
    complexity of a fully tracked fourth execution phase.
    """
    tag_type = registry[UNIVERSAL_TAG_TYPE]
    tag_endpoint = resolve_endpoint(target_client.nb, tag_type.endpoint)
    target_lookup = LiveTargetLookup(target_client)

    existing_tag = target_lookup.find(tag_type, {"slug": tag_slug})
    if existing_tag is not None:
        marker_tag_id = existing_tag["id"]
    else:
        created_tag = target_client.create(tag_endpoint, {"slug": tag_slug, "name": tag_slug})
        marker_tag_id = created_tag.id if hasattr(created_tag, "id") else created_tag["id"]

    created_items = db.execute(
        select(MigrationJobItem).where(
            MigrationJobItem.job_id == job.id,
            MigrationJobItem.planned_action == "create",
            MigrationJobItem.execution_status == "done",
            MigrationJobItem.object_type != UNIVERSAL_TAG_TYPE,  # don't tag the tags themselves
        )
    ).scalars().all()
    for item in created_items:
        type_spec = registry[item.object_type]
        fk_refs = json.loads(item.fk_refs_json)
        resolved_fk, unresolved = resolve_fk_refs(type_spec, fk_refs, id_map=id_map)
        if unresolved:
            continue  # shouldn't happen for an item already "done", but tagging must never crash the job
        existing_tags = resolved_fk.get(UNIVERSAL_TAG_FIELD) or []
        if marker_tag_id in existing_tags:
            continue
        endpoint = resolve_endpoint(target_client.nb, type_spec.endpoint)
        try:
            target_client.update_by_id(endpoint, item.target_id, {UNIVERSAL_TAG_FIELD: [*existing_tags, marker_tag_id]})
        except MigrationApiError:
            pass  # best-effort — a tagging failure must never fail the migration itself


def execute_job(
    db: Session,
    job: MigrationJob,
    *,
    registry: Registry,
    target_client: RateLimitedClient,
    fail_fast: bool = False,
    marker_tag_slug: str | None = None,
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
    _refresh_live_totals(db, job)

    id_map = _load_id_map(db, job.id)

    try:
        if job.phase == "primary":
            _execute_pending_items(
                db, job, registry=registry, target_client=target_client,
                id_map=id_map, fail_fast=fail_fast,
            )
            has_pending_items = db.execute(
                select(MigrationJobItem.id).where(
                    MigrationJobItem.job_id == job.id,
                    MigrationJobItem.execution_status == "pending",
                ).limit(1)
            ).first() is not None
            if not (fail_fast and has_pending_items):
                job.phase = "patch"
                db.commit()
        if job.phase == "patch":
            _execute_pending_patches(
                db, job, registry=registry, target_client=target_client,
                id_map=id_map, fail_fast=fail_fast,
            )
            has_pending_patches = db.execute(
                select(MigrationJobPatch.id).where(
                    MigrationJobPatch.job_id == job.id,
                    MigrationJobPatch.execution_status == "pending",
                ).limit(1)
            ).first() is not None
            if not (fail_fast and has_pending_patches):
                job.phase = "done"
                db.commit()
                if marker_tag_slug:
                    # Marker tagging is a best-effort nicety, never a reason the whole
                    # migration fails to reach a terminal status — a failure here (e.g. the
                    # target rejects tag creation) is recorded as a warning and the job still
                    # completes normally.
                    try:
                        _apply_marker_tag(db, job, registry=registry, target_client=target_client, id_map=id_map, tag_slug=marker_tag_slug)
                    except Exception as exc:  # noqa: BLE001 — see comment above
                        warnings = json.loads(job.warnings_json or "[]")
                        warnings.append(f"Marker tagging failed and was skipped: {exc}")
                        job.warnings_json = json.dumps(warnings)
                        db.commit()
    except JobCancelled:
        _refresh_live_totals(db, job)
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

    _refresh_live_totals(db, job)
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
