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
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MigrationJob, MigrationJobItem, MigrationJobPatch
from app.services.migration.client import MigrationApiError, RateLimitedClient
from app.services.migration.matcher import AmbiguousTargetLookup, IdMap
from app.services.migration.planner import LiveTargetLookup, resolve_endpoint
from app.services.migration.registry import UNIVERSAL_TAG_FIELD, UNIVERSAL_TAG_TYPE, Registry, TypeSpec
from app.services.migration.sanitize import normalize_write_payload, resolve_fk_refs, resolve_polymorphic_fk_refs
from app.services.syslog_client import send_audit_entry
from app.timeutil import utcnow

HEARTBEAT_EVERY = 10  # items/patches between last_heartbeat_at updates — cheap enough to not throttle a large run
LIVE_TOTALS_EVERY = HEARTBEAT_EVERY  # keep progress and cancellation bookkeeping on one predictable cadence
BULK_CREATE_SIZE = 100
PENDING_FETCH_SIZE = 100


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
        job.last_heartbeat_at = utcnow()
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


def _refresh_live_totals_if_due(
    db: Session,
    job: MigrationJob,
    processed: int,
    last_refresh: int,
    *,
    force: bool = False,
) -> int:
    """Refresh durable progress at a bounded cadence, or unconditionally at a phase boundary."""
    # UI counters may lag by at most one cadence during a busy phase. Avoiding
    # per-object aggregate queries is worth that small display-only delay.
    if force or processed - last_refresh >= LIVE_TOTALS_EVERY:
        _refresh_live_totals(db, job)
        return processed
    return last_refresh


def _execute_one_item(
    db: Session,
    item: MigrationJobItem,
    *,
    registry: Registry,
    target_client: RateLimitedClient,
    target_lookup: LiveTargetLookup,
    id_map: IdMap,
    marker_tag_ids: dict[str, int],
) -> None:
    type_spec = registry[item.object_type]
    static_fields = json.loads(item.payload_json)
    fk_refs = json.loads(item.fk_refs_json)

    resolved_fk, unresolved_fk = resolve_fk_refs(type_spec, fk_refs, id_map=id_map)
    if unresolved_fk:
        item.execution_status = "error"
        item.error_detail = f"Required dependency never resolved (skipped or ambiguous?): {sorted(unresolved_fk)}"
        item.executed_at = utcnow()
        db.commit()
        return

    marker_tag_id = None if item.object_type == UNIVERSAL_TAG_TYPE else marker_tag_ids.get(item.planned_action)
    payload = normalize_write_payload(
        type_spec, _with_marker_tag({**static_fields, **resolved_fk}, marker_tag_id),
    )
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
            item.target_natural_key = item.target_natural_key or item.source_natural_key
            item.execution_status = "done"
            item.executed_at = utcnow()
            db.commit()
            id_map.put(item.object_type, item.source_id, item.target_id)
            return
        created = target_client.create(endpoint, payload)
        item.target_id = created.id if hasattr(created, "id") else created["id"]
        item.target_natural_key = item.source_natural_key
    elif item.planned_action == "update":
        # target_id is already set (it was the matched object's real id since plan time) —
        # restored after the bulk-create refactor accidentally dropped this branch, which
        # left every conflict_policy="update" item hitting the else below and crashing the
        # whole job with an uncaught AssertionError.
        target_client.update_by_id(endpoint, item.target_id, payload)
    else:
        raise AssertionError(f"_execute_one_item called for non-executable action {item.planned_action!r}")

    item.execution_status = "done"
    item.executed_at = utcnow()
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
    for filters in _resolved_match_filters(type_spec, static_fields, resolved_fk):
        found = target_lookup.find(type_spec, filters)
        if found is not None:
            return found["id"]
    return None


def _resolved_match_filters(type_spec: TypeSpec, static_fields: dict, resolved_fk: dict) -> list[dict[str, Any]]:
    """Build target-side natural-key filters from an execution-ready payload."""
    result: list[dict[str, Any]] = []
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
                # Keep execution's duplicate-prevention lookup identical to
                # plan-time matching: blank optional identifiers are absent,
                # not natural keys.  Otherwise all device types from one
                # manufacturer with part_number="" can collapse onto the
                # first type created in the batch.
                if value is None or (isinstance(value, str) and not value.strip()):
                    matchable = False
                    break
                filters[field_name] = value
        if not matchable:
            continue
        result.append(filters)
    return result


def _execution_source_object(item: MigrationJobItem, type_spec: TypeSpec) -> dict[str, Any]:
    """Reconstruct the source values ``LiveTargetLookup.prepare`` needs from a durable plan row."""
    source_obj = json.loads(item.payload_json)
    for field_name, source_id in json.loads(item.fk_refs_json).items():
        list_spec = type_spec.polymorphic_list_field_map.get(field_name)
        if list_spec and isinstance(source_id, list):
            source_obj[field_name] = [
                {
                    list_spec.discriminator_field: ref.get("discriminator"),
                    list_spec.object_id_field: ref.get("id"),
                }
                for ref in source_id
            ]
        elif field_name in type_spec.polymorphic_field_map and isinstance(source_id, dict):
            source_obj[field_name] = source_id.get("id")
        elif isinstance(source_id, list):
            source_obj[field_name] = [{"id": value} for value in source_id]
        else:
            source_obj[field_name] = {"id": source_id}
    return source_obj


def _remember_created_match(
    type_spec: TypeSpec,
    static_fields: dict,
    resolved_fk: dict,
    target_id: int,
    target_lookup: LiveTargetLookup,
) -> None:
    target_obj = {"id": target_id, **static_fields, **resolved_fk}
    for filters in _resolved_match_filters(type_spec, static_fields, resolved_fk):
        target_lookup.remember(type_spec, filters, target_obj)


def _with_marker_tag(payload: dict[str, Any], marker_tag_id: int | None) -> dict[str, Any]:
    """Append an execution marker without discarding or duplicating source tags."""
    if marker_tag_id is None:
        return payload
    tags = list(payload.get(UNIVERSAL_TAG_FIELD) or [])
    if marker_tag_id not in tags:
        tags.append(marker_tag_id)
    return {**payload, UNIVERSAL_TAG_FIELD: tags}


def _mark_done(
    db: Session, item: MigrationJobItem, target_id: int, id_map: IdMap, *, commit: bool = True,
) -> None:
    item.target_id = target_id
    item.target_natural_key = item.target_natural_key or item.source_natural_key
    item.execution_status = "done"
    item.executed_at = utcnow()
    if commit:
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
    marker_tag_ids: dict[str, int],
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
            item.executed_at = utcnow()
            db.commit()
            if fail_fast:
                return True
            continue
        try:
            existing_id = _find_existing_by_natural_key(type_spec, static_fields, resolved_fk, target_lookup)
        except AmbiguousTargetLookup as exc:
            item.execution_status = "error"
            item.error_detail = str(exc)
            item.executed_at = utcnow()
            db.commit()
            if fail_fast:
                return True
            continue
        if existing_id is not None:
            _mark_done(db, item, existing_id, id_map)
            continue
        marker_tag_id = None if item.object_type == UNIVERSAL_TAG_TYPE else marker_tag_ids.get(item.planned_action)
        payload = normalize_write_payload(
            type_spec, _with_marker_tag({**static_fields, **resolved_fk}, marker_tag_id),
        )
        prepared.append((item, payload, payload, type_spec))

    if not prepared:
        return False
    endpoint = resolve_endpoint(target_client.nb, prepared[0][3].endpoint)
    try:
        created_records = target_client.create_many(endpoint, [payload for _, payload, _, _ in prepared])
    except MigrationApiError:
        # NetBox rejects a whole bulk request when one or more rows are invalid.
        # Retry the prepared rows individually so valid cables/objects still
        # migrate and each bad row retains its own actionable API error.
        target_lookup.clear_cache()
        for item, payload, _, type_spec in prepared:
            static_fields = json.loads(item.payload_json)
            resolved_fk, _ = resolve_fk_refs(type_spec, json.loads(item.fk_refs_json), id_map=id_map)
            existing_id = _find_existing_by_natural_key(type_spec, static_fields, resolved_fk, target_lookup)
            if existing_id is not None:
                _mark_done(db, item, existing_id, id_map)
                continue
            try:
                created = target_client.create(endpoint, payload)
            except MigrationApiError as item_exc:
                item.execution_status = "error"
                item.error_detail = str(item_exc)
                item.executed_at = utcnow()
                db.commit()
                if fail_fast:
                    break
                continue
            target_id = created.id if hasattr(created, "id") else created.get("id")
            if target_id is None:
                item.execution_status = "error"
                item.error_detail = "Create response did not contain an id after bulk-create fallback."
                item.executed_at = utcnow()
                db.commit()
                if fail_fast:
                    break
                continue
            _remember_created_match(type_spec, static_fields, resolved_fk, target_id, target_lookup)
            _mark_done(db, item, target_id, id_map)
        return fail_fast

    for (item, payload, _, _), created in zip(prepared, created_records):
        target_id = created.id if hasattr(created, "id") else created.get("id")
        if target_id is None:
            item.execution_status = "error"
            item.error_detail = "Bulk create response did not contain an id."
            item.executed_at = utcnow()
            db.commit()
            if fail_fast:
                return True
            continue
        static_fields = json.loads(item.payload_json)
        resolved_fk, _ = resolve_fk_refs(
            registry[item.object_type], json.loads(item.fk_refs_json), id_map=id_map,
        )
        _remember_created_match(registry[item.object_type], static_fields, resolved_fk, target_id, target_lookup)
        _mark_done(db, item, target_id, id_map, commit=False)
    # The remote batch has already succeeded. Persist all returned ids together;
    # if the process stops before this commit, resume rechecks natural keys and
    # maps the existing target objects instead of creating duplicates.
    db.commit()
    return False


def _execute_pending_items(
    db: Session,
    job: MigrationJob,
    *,
    registry: Registry,
    target_client: RateLimitedClient,
    id_map: IdMap,
    fail_fast: bool,
    marker_tag_ids: dict[str, int],
    max_batch_size: int,
) -> None:
    target_lookup = LiveTargetLookup(target_client)
    counter = 0
    last_totals_refresh = 0
    pending_rows = db.execute(select(MigrationJobItem.object_type).where(
        MigrationJobItem.job_id == job.id, MigrationJobItem.execution_status == "pending",
    )).all()
    type_totals: dict[str, int] = {}
    type_done: dict[str, int] = {}
    prepared_types: set[str] = set()
    for (type_key,) in pending_rows:
        type_totals[type_key] = type_totals.get(type_key, 0) + 1
    while True:
        # One worker owns a job's execution in this single-process service, so no
        # other executor legitimately mutates these rows concurrently. Re-query at
        # each window boundary to retain safe retry/resume behavior without one
        # SELECT ... LIMIT 1 round-trip per object.
        pending_items = db.execute(
            select(MigrationJobItem)
            .where(
                MigrationJobItem.job_id == job.id,
                MigrationJobItem.execution_status == "pending",
            )
            .order_by(MigrationJobItem.order_index)
            .limit(PENDING_FETCH_SIZE)
        ).scalars().all()
        if not pending_items:
            _refresh_live_totals_if_due(db, job, counter, last_totals_refresh, force=True)
            return
        window_index = 0
        while window_index < len(pending_items):
            item = pending_items[window_index]
            counter += 1
            label = item.object_type.split(".")[-1].replace("_", " ").title()
            verb = "Creating" if item.planned_action == "create" else "Updating"
            next_index = type_done.get(item.object_type, 0) + 1
            job.current_step = f"{verb} {label} ({next_index}/{type_totals[item.object_type]})"
            db.commit()
            _check_cancelled(db, job, counter)
            if item.planned_action == "create":
                if max_batch_size == 0:
                    _execute_one_item(
                        db, item, registry=registry, target_client=target_client,
                        target_lookup=target_lookup, id_map=id_map, marker_tag_ids=marker_tag_ids,
                    )
                    window_index += 1
                    type_done[item.object_type] = type_done.get(item.object_type, 0) + 1
                    continue
                if item.object_type not in prepared_types:
                    type_spec = registry[item.object_type]
                    type_items = db.execute(
                        select(MigrationJobItem)
                        .where(
                            MigrationJobItem.job_id == job.id,
                            MigrationJobItem.execution_status == "pending",
                            MigrationJobItem.object_type == item.object_type,
                            MigrationJobItem.planned_action == "create",
                        )
                        .order_by(MigrationJobItem.order_index)
                    ).scalars().all()
                    # This is the real execution id_map rebuilt from durable done rows, never
                    # the planner's negative-placeholder map. Strategies whose same-type FK is
                    # not resolved yet are deliberately omitted by prepare and retain find()'s
                    # live fallback after an earlier item creates that dependency.
                    target_lookup.prepare(
                        type_spec, [_execution_source_object(candidate, type_spec) for candidate in type_items],
                        registry=registry, id_map=id_map,
                    )
                    prepared_types.add(item.object_type)
                contiguous: list[MigrationJobItem] = []
                seen_match_keys: set[tuple[str, tuple]] = set()
                batch_size = min(BULK_CREATE_SIZE, max_batch_size)
                for candidate in pending_items[window_index:window_index + batch_size]:
                    if candidate.object_type != item.object_type or candidate.planned_action != "create":
                        break
                    candidate_spec = registry[candidate.object_type]
                    static_fields = json.loads(candidate.payload_json)
                    resolved_fk, unresolved_fk = resolve_fk_refs(
                        candidate_spec, json.loads(candidate.fk_refs_json), id_map=id_map,
                    )
                    # A later same-type item may depend on an earlier item in this run. End
                    # the bulk-create batch so the earlier item's real id reaches id_map first;
                    # the next pass can then evaluate the dependent's natural key correctly.
                    if contiguous and unresolved_fk:
                        break
                    match_keys = {
                        target_lookup.match_key(candidate_spec, filters)
                        for filters in _resolved_match_filters(candidate_spec, static_fields, resolved_fk)
                    }
                    # Two source rows with the same natural key cannot be sent in one create
                    # request: after the first is created, the second safety recheck must map
                    # to it. Splitting here plus remember() keeps the prepared cache current.
                    if contiguous and seen_match_keys & match_keys:
                        break
                    contiguous.append(candidate)
                    seen_match_keys.update(match_keys)
                if _execute_create_batch(
                    db, contiguous, registry=registry, target_client=target_client,
                    target_lookup=target_lookup, id_map=id_map, fail_fast=fail_fast,
                    marker_tag_ids=marker_tag_ids,
                ):
                    _refresh_live_totals_if_due(db, job, counter, last_totals_refresh, force=True)
                    return
                counter += len(contiguous) - 1
                window_index += len(contiguous)
                type_done[item.object_type] = type_done.get(item.object_type, 0) + len(contiguous)
                job.current_step = f"Creating {label} ({type_done[item.object_type]}/{type_totals[item.object_type]})"
                db.commit()
                last_totals_refresh = _refresh_live_totals_if_due(
                    db, job, counter, last_totals_refresh,
                )
                continue
            try:
                _execute_one_item(
                    db, item, registry=registry, target_client=target_client,
                    target_lookup=target_lookup, id_map=id_map, marker_tag_ids=marker_tag_ids,
                )
            except (MigrationApiError, AmbiguousTargetLookup) as exc:
                item.execution_status = "error"
                item.error_detail = str(exc)
                item.executed_at = utcnow()
                db.commit()
                if fail_fast:
                    _refresh_live_totals_if_due(db, job, counter, last_totals_refresh, force=True)
                    return
            window_index += 1
            type_done[item.object_type] = type_done.get(item.object_type, 0) + 1
            last_totals_refresh = _refresh_live_totals_if_due(
                db, job, counter, last_totals_refresh,
            )


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
    last_totals_refresh = 0
    total = len(db.execute(select(MigrationJobPatch.id).where(
        MigrationJobPatch.job_id == job.id, MigrationJobPatch.execution_status == "pending",
    )).all())
    while True:
        pending_patches = db.execute(
            select(MigrationJobPatch)
            .where(MigrationJobPatch.job_id == job.id, MigrationJobPatch.execution_status == "pending")
            .order_by(MigrationJobPatch.order_index)
            .limit(PENDING_FETCH_SIZE)
        ).scalars().all()
        if not pending_patches:
            _refresh_live_totals_if_due(db, job, counter, last_totals_refresh, force=True)
            return
        for patch in pending_patches:
            counter += 1
            job.current_step = f"Applying deferred patches ({counter}/{total})"
            db.commit()
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
                patch.executed_at = utcnow()
                db.commit()
                last_totals_refresh = _refresh_live_totals_if_due(
                    db, job, counter, last_totals_refresh,
                )
                if fail_fast:
                    _refresh_live_totals_if_due(db, job, counter, last_totals_refresh, force=True)
                    return
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
            patch.executed_at = utcnow()
            db.commit()
            last_totals_refresh = _refresh_live_totals_if_due(
                db, job, counter, last_totals_refresh,
            )
            if fail_fast and patch.execution_status == "error":
                _refresh_live_totals_if_due(db, job, counter, last_totals_refresh, force=True)
                return


def _prepare_marker_tags(
    db: Session,
    job: MigrationJob,
    *,
    registry: Registry,
    target_client: RateLimitedClient,
    tag_slug: str,
) -> dict[str, int]:
    """Create/reuse the create/update marker tags before object execution.

    Tag setup is deliberately best-effort. Each successful tag is returned
    independently so a failure to prepare one marker does not prevent either
    the migration or use of the other marker.
    """
    tag_type = registry[UNIVERSAL_TAG_TYPE]
    tag_endpoint = resolve_endpoint(target_client.nb, tag_type.endpoint)
    target_lookup = LiveTargetLookup(target_client)
    marker_ids: dict[str, int] = {}
    warnings = json.loads(job.warnings_json or "[]")
    for action, suffix in (("create", "created"), ("update", "changed")):
        slug = f"{tag_slug}-{suffix}"
        try:
            existing_tag = target_lookup.find(tag_type, {"slug": slug})
            if existing_tag is not None:
                marker_ids[action] = existing_tag["id"]
            else:
                created_tag = target_client.create(tag_endpoint, {"slug": slug, "name": slug})
                marker_ids[action] = created_tag.id if hasattr(created_tag, "id") else created_tag["id"]
        except Exception as exc:  # noqa: BLE001 - markers must never block the migration
            warnings.append(f"{suffix.title()} marker tag setup failed and was skipped: {exc}")
    job.warnings_json = json.dumps(warnings)
    db.commit()
    return marker_ids


def execute_job(
    db: Session,
    job: MigrationJob,
    *,
    registry: Registry,
    target_client: RateLimitedClient,
    fail_fast: bool = False,
    marker_tag_slug: str | None = None,
    max_batch_size: int = BULK_CREATE_SIZE,
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
    job.current_step = "Preparing migration execution"
    if job.started_at is None:
        job.started_at = utcnow()
    db.commit()
    _refresh_live_totals(db, job)

    id_map = _load_id_map(db, job.id)
    marker_tag_ids: dict[str, int] = {}
    if marker_tag_slug:
        job.current_step = "Preparing migration marker tags"
        db.commit()
        marker_tag_ids = _prepare_marker_tags(
            db, job, registry=registry, target_client=target_client, tag_slug=marker_tag_slug,
        )

    try:
        if job.phase == "primary":
            _execute_pending_items(
                db, job, registry=registry, target_client=target_client,
                id_map=id_map, fail_fast=fail_fast, marker_tag_ids=marker_tag_ids,
                max_batch_size=max(0, max_batch_size),
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
    except JobCancelled:
        _refresh_live_totals(db, job)
        job.status = "cancelled"
        job.current_step = "Cancelled"
        job.finished_at = utcnow()
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
    job.current_step = "Completed with errors" if any_errors else "Completed"
    job.finished_at = utcnow()
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
