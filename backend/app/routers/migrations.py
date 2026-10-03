"""
Endpoints for the instance-to-instance data migration feature.

Execution runs in a background thread (this app is a single process with no
task queue — see the module docstring in services/migration/executor.py),
committing after every object so a crash leaves a resumable, not corrupted,
job. An orphaned "running" job found at startup (see app.main's startup
hook) is unambiguously abandoned in a single-process deployment, so it is
always safe to resume it there without any lease/heartbeat comparison.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.auth import get_current_actor
from app.database import SessionLocal, get_db
from app.rbac import AccessContext, filter_scoped, require_resource_admin, require_role, require_visible
from app.services.migration import registry as registry_mod
from app.services.migration.client import build_client
from app.services.migration.executor import execute_job
from app.services.migration.matcher import MappingAction, MappingOverride, natural_key_label
from app.services.migration.mapping_skeleton import build_mapping_skeleton
from app.services.migration.planner import build_plan, persist_plan, resolve_endpoint
from app.services.migration.report import build_report, render_html, render_json
from app.services.migration.rollback import rollback_job
from app.services.netbox_client import check_migration_version_compatibility, test_connection

router = APIRouter(prefix="/api/migrations", tags=["migrations"])


def _decode_mapping_overrides(raw: dict[str, schemas.MigrationMappingOverride]) -> dict[tuple[str, int], MappingOverride]:
    decoded: dict[tuple[str, int], MappingOverride] = {}
    for key, override in raw.items():
        try:
            type_key, source_id_str = key.rsplit(":", 1)
            source_id = int(source_id_str)
        except ValueError as exc:
            raise HTTPException(400, f"Invalid mapping_overrides key {key!r}; expected 'type.key:source_id'.") from exc
        try:
            action = MappingAction(override.action)
        except ValueError as exc:
            raise HTTPException(400, f"Invalid mapping action {override.action!r} for {key}.") from exc
        if action == MappingAction.MAP and override.target_id is None:
            raise HTTPException(400, f"Mapping override for {key} has action=map but no target_id.")
        decoded[(type_key, source_id)] = MappingOverride(action=action, target_id=override.target_id)
    return decoded


def _get_instance_or_404(db: Session, instance_id: str) -> models.NetboxInstance:
    instance = db.get(models.NetboxInstance, instance_id)
    if instance is None:
        raise HTTPException(404, "Instance not found.")
    return instance


def _build_clients(db: Session, source_id: str, target_id: str, max_rps: float):
    source_instance = _get_instance_or_404(db, source_id)
    target_instance = _get_instance_or_404(db, target_id)
    source_client = build_client(
        source_instance.base_url, crypto.decrypt(source_instance.api_token_encrypted),
        source_instance.verify_ssl, read_only=True, max_requests_per_second=max_rps,
    )
    target_client = build_client(
        target_instance.base_url, crypto.decrypt(target_instance.api_token_encrypted),
        target_instance.verify_ssl, read_only=False, max_requests_per_second=max_rps,
    )
    return source_client, target_client


def _job_summary(db: Session, job: models.MigrationJob) -> schemas.MigrationJobSummary:
    source = db.get(models.NetboxInstance, job.source_instance_id)
    target = db.get(models.NetboxInstance, job.target_instance_id)
    return schemas.MigrationJobSummary(
        id=job.id, source_instance_id=job.source_instance_id, target_instance_id=job.target_instance_id,
        source_instance_name=source.name if source else None, target_instance_name=target.name if target else None,
        status=job.status, phase=job.phase, current_step=job.current_step,
        tenant_filter=json.loads(job.tenant_filter_json), selected_types=json.loads(job.selected_types_json),
        totals=json.loads(job.totals_json), warnings=json.loads(job.warnings_json),
        created_at=job.created_at, started_at=job.started_at, last_heartbeat_at=job.last_heartbeat_at,
        finished_at=job.finished_at, actor_name=job.actor_name,
    )


def _require_job_visibility(job: models.MigrationJob, ctx: AccessContext, db: Session) -> None:
    # Migration metadata contains identifiers and links from both instances; visibility of
    # only one side must not disclose the other side through a job endpoint.
    require_visible("instance", job.source_instance_id, ctx, db)
    require_visible("instance", job.target_instance_id, ctx, db)


@router.get("/types")
def list_types(_: AccessContext = Depends(require_role("viewer"))):
    """Selectable types and their UI-visible transitive dependency relationships."""
    from app.services.migration.report import display_name

    registry = registry_mod.load_registry()
    result = []
    for key in registry.selectable_types():
        required, possible_optional = registry.selectable_dependency_relationships(key)
        result.append({
            "type": key,
            "label": display_name(key),
            "dependencies": list(registry[key].dependencies),
            "optional_dependencies": list(registry[key].optional_dependencies),
            "required_selectable_dependencies": required,
            "possible_optional_selectable_dependencies": possible_optional,
        })
    return result


@router.get("/instances/{instance_id}/tenants")
def list_instance_tenants(
    instance_id: str,
    q: str = Query(default="", max_length=100),
    db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("viewer")),
):
    """Return tenants from a migration source using the read-only, paginated client."""
    require_visible("instance", instance_id, ctx, db)
    instance = _get_instance_or_404(db, instance_id)
    client = build_client(
        instance.base_url, crypto.decrypt(instance.api_token_encrypted), instance.verify_ssl,
        read_only=True,
    )
    filters = {"q": q.strip()} if q.strip() else {}
    try:
        return [
            {"id": int(row["id"]), "name": str(row["name"]), "slug": str(row["slug"])}
            for row in client.paginated(client.nb.tenancy.tenants, **filters)
        ]
    except Exception as exc:  # noqa: BLE001 - surface the source API failure as a gateway error
        raise HTTPException(502, f"Could not load tenants from the source instance: {exc}") from exc


@router.post("/plan", response_model=schemas.MigrationJobSummary)
def plan_migration(
    payload: schemas.MigrationPlanRequest, request: Request,
    db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    actor = get_current_actor(request)
    require_visible("instance", payload.source_instance_id, ctx, db)
    require_resource_admin(ctx, "instance", payload.target_instance_id, db)

    registry = registry_mod.load_registry()
    unknown = set(payload.selected_types) - set(registry.selectable_types())
    if unknown:
        raise HTTPException(400, f"Unknown or non-selectable type(s): {sorted(unknown)}")

    _get_instance_or_404(db, payload.source_instance_id)
    _get_instance_or_404(db, payload.target_instance_id)
    # Validate the user-supplied mapping syntax before returning an accepted job;
    # all network and planning work happens in the worker below.
    _decode_mapping_overrides(payload.mapping_overrides)

    if payload.job_id:
        job = db.get(models.MigrationJob, payload.job_id)
        if job is None:
            raise HTTPException(404, "Migration job not found.")
        if (job.source_instance_id, job.target_instance_id) != (payload.source_instance_id, payload.target_instance_id):
            raise HTTPException(400, "A re-plan must keep the original source and target instances.")
        if job.status != "planned" and not (job.status == "failed" and job.started_at is None):
            raise HTTPException(409, f"Cannot re-plan a job in status={job.status!r}; only a plan that has not executed can be edited.")
    else:
        job = models.MigrationJob(
            source_instance_id=payload.source_instance_id, target_instance_id=payload.target_instance_id,
            actor_sub=actor.get("sub"), actor_name=actor.get("name"), actor_email=actor.get("email"),
            status="planning",
        )
        db.add(job)

    job.tenant_filter_json = json.dumps(payload.tenant_filter)
    job.selected_types_json = json.dumps(payload.selected_types)
    job.resolved_types_json = "[]"
    job.conflict_policy_json = json.dumps(payload.conflict_policy)
    job.mapping_json = json.dumps({k: v.model_dump() for k, v in payload.mapping_overrides.items()})
    job.options_json = json.dumps({
        "include_untenanted": payload.include_untenanted, "marker_tag": payload.marker_tag,
        "fail_fast": payload.fail_fast,
        "max_requests_per_second": payload.max_requests_per_second,
    })
    job.totals_json = "{}"
    job.warnings_json = "[]"
    job.status = "planning"
    job.phase = "primary"
    job.current_step = "Starting planning"
    job.finished_at = None
    db.commit()
    db.refresh(job)

    summary = _job_summary(db, job)
    thread = threading.Thread(target=_run_plan_in_background, args=(job.id,), daemon=True)
    thread.start()
    return summary


def _run_plan_in_background(job_id: str) -> None:
    """Build and persist a plan with its own DB session, surfacing failures on the job."""
    db = SessionLocal()
    try:
        job = db.get(models.MigrationJob, job_id)
        if job is None or job.status != "planning":
            return

        def update_progress(message: str) -> None:
            job.current_step = message
            job.last_heartbeat_at = datetime.utcnow()
            db.commit()

        update_progress("Checking NetBox version compatibility")
        source_instance = _get_instance_or_404(db, job.source_instance_id)
        target_instance = _get_instance_or_404(db, job.target_instance_id)
        source_version_result, _, version_warnings = check_migration_version_compatibility(
            source_instance.base_url, crypto.decrypt(source_instance.api_token_encrypted), source_instance.verify_ssl,
            target_instance.base_url, crypto.decrypt(target_instance.api_token_encrypted), target_instance.verify_ssl,
        )
        options = json.loads(job.options_json or "{}")
        source_client, target_client = _build_clients(
            db, job.source_instance_id, job.target_instance_id,
            options.get("max_requests_per_second", 4.0),
        )
        raw_mapping = json.loads(job.mapping_json or "{}")
        mapping_overrides = {
            tuple_key: MappingOverride(action=MappingAction(value["action"]), target_id=value.get("target_id"))
            for key, value in raw_mapping.items()
            for tuple_key in [(key.rsplit(":", 1)[0], int(key.rsplit(":", 1)[1]))]
        }
        plan = build_plan(
            registry=registry_mod.load_registry(), source_client=source_client, target_client=target_client,
            selected_types=set(json.loads(job.selected_types_json)),
            tenant_filter=json.loads(job.tenant_filter_json), mapping_overrides=mapping_overrides,
            conflict_policy=json.loads(job.conflict_policy_json or "{}"),
            include_untenanted=bool(options.get("include_untenanted", False)),
            source_netbox_version=source_version_result.get("netbox_version"), progress=update_progress,
        )
        plan.warnings.extend(version_warnings)
        update_progress("Saving migration plan")
        persist_plan(db, job.id, plan)
        job.resolved_types_json = json.dumps(plan.resolved_types)
        job.totals_json = json.dumps(plan.totals)
        job.warnings_json = json.dumps(plan.warnings)
        job.status = "planned"
        job.current_step = "Planning complete"
        job.last_heartbeat_at = datetime.utcnow()
        db.commit()
    except Exception as exc:  # noqa: BLE001 - no request exists to receive worker failures
        db.rollback()
        job = db.get(models.MigrationJob, job_id)
        if job is not None:
            job.status = "failed"
            job.current_step = f"Planning failed: {exc}"
            job.warnings_json = json.dumps([*json.loads(job.warnings_json or "[]"), str(exc)])
            job.finished_at = datetime.utcnow()
            db.commit()
        import logging
        logging.getLogger(__name__).exception("Migration planning job %s failed", job_id)
    finally:
        db.close()


@router.post("/preflight", response_model=schemas.MigrationPreflightResponse)
def migration_preflight(
    payload: schemas.MigrationPreflightRequest,
    db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("viewer")),
):
    require_visible("instance", payload.source_instance_id, ctx, db)
    require_visible("instance", payload.target_instance_id, ctx, db)
    source = _get_instance_or_404(db, payload.source_instance_id)
    target = _get_instance_or_404(db, payload.target_instance_id)
    source_result = test_connection(
        source.base_url, crypto.decrypt(source.api_token_encrypted), source.verify_ssl,
    )
    target_result = test_connection(
        target.base_url, crypto.decrypt(target.api_token_encrypted), target.verify_ssl,
    )
    return schemas.MigrationPreflightResponse(
        source=schemas.MigrationPreflightSide(
            reachable=bool(source_result.get("netbox_version")),
            token_valid=bool(source_result.get("ok")),
            netbox_version=source_result.get("netbox_version"),
            detail=source_result.get("detail"),
        ),
        target=schemas.MigrationPreflightSide(
            reachable=bool(target_result.get("netbox_version")),
            token_valid=bool(target_result.get("ok")),
            netbox_version=target_result.get("netbox_version"),
            detail=target_result.get("detail"),
            write_permission_checked=False,
            write_permission_ok=None,
        ),
    )


@router.get("/jobs", response_model=list[schemas.MigrationJobSummary])
def list_jobs(db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("viewer"))):
    jobs = db.query(models.MigrationJob).order_by(models.MigrationJob.created_at.desc()).all()
    # This list previously bypassed the instance visibility checks used by planning.
    jobs = filter_scoped(jobs, "instance", ctx, db, id_attr="target_instance_id")[:100]
    return [_job_summary(db, job) for job in jobs]


@router.get("/jobs/{job_id}", response_model=schemas.MigrationJobSummary)
def get_job(job_id: str, db: Session = Depends(get_db), _: AccessContext = Depends(require_role("viewer"))):
    job = db.get(models.MigrationJob, job_id)
    if job is None:
        raise HTTPException(404, "Migration job not found.")
    # Keep job details aligned with plan_migration's existing instance scoping.
    _require_job_visibility(job, _, db)
    return _job_summary(db, job)


@router.get("/jobs/{job_id}/mapping-skeleton", response_model=list[schemas.MigrationMappingSkeletonRow])
def get_mapping_skeleton(
    job_id: str, db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("viewer")),
):
    job = db.get(models.MigrationJob, job_id)
    if job is None:
        raise HTTPException(404, "Migration job not found.")
    if job.status == "planning":
        raise HTTPException(409, "The mapping skeleton is not available until planning has finished.")
    _require_job_visibility(job, ctx, db)
    registry = registry_mod.load_registry()
    items = (
        db.query(models.MigrationJobItem)
        .filter(models.MigrationJobItem.job_id == job_id)
        .order_by(models.MigrationJobItem.order_index)
        .all()
    )
    return [schemas.MigrationMappingSkeletonRow(**row.__dict__) for row in build_mapping_skeleton(items, registry)]


@router.get("/jobs/{job_id}/target-options", response_model=list[schemas.MigrationTargetOption])
def get_target_options(
    job_id: str,
    object_type: str,
    q: str = Query(default="", max_length=100),
    db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("viewer")),
):
    """Return human-labelled target objects for a manual mapping dropdown."""
    job = db.get(models.MigrationJob, job_id)
    if job is None:
        raise HTTPException(404, "Migration job not found.")
    _require_job_visibility(job, ctx, db)
    registry = registry_mod.load_registry()
    if object_type not in registry:
        raise HTTPException(400, f"Unknown migration object type {object_type!r}.")
    target = _get_instance_or_404(db, job.target_instance_id)
    options = json.loads(job.options_json or "{}")
    client = build_client(
        target.base_url, crypto.decrypt(target.api_token_encrypted), target.verify_ssl,
        read_only=True, max_requests_per_second=options.get("max_requests_per_second", 4.0),
    )
    type_spec = registry[object_type]
    endpoint = resolve_endpoint(client.nb, type_spec.endpoint)
    filters = {"q": q.strip()} if q.strip() else {}
    result = []
    for row in client.paginated(endpoint, **filters):
        target_id = row.get("id")
        if type(target_id) is not int or target_id <= 0:
            continue
        result.append(schemas.MigrationTargetOption(
            id=target_id,
            label=natural_key_label(row, type_spec=type_spec) or f"#{target_id}",
        ))
    return result


def _marker_tag_slug(source_instance: models.NetboxInstance | None) -> str:
    """e.g. "Primary DC" -> "migrated-from-primary-dc". No external slugify dependency needed for this narrow use."""
    name = (source_instance.name if source_instance else "unknown-source").strip().lower()
    slug_body = re.sub(r"[^a-z0-9]+", "-", name).strip("-") or "unknown-source"
    return f"migrated-from-{slug_body}"


def _run_in_background(job_id: str, max_rps: float) -> None:
    db = SessionLocal()
    try:
        job = db.get(models.MigrationJob, job_id)
        if job is None:
            return
        registry = registry_mod.load_registry()
        target_instance = db.get(models.NetboxInstance, job.target_instance_id)
        target_client = build_client(
            target_instance.base_url, crypto.decrypt(target_instance.api_token_encrypted),
            target_instance.verify_ssl, read_only=False, max_requests_per_second=max_rps,
        )
        options = json.loads(job.options_json or "{}")
        marker_tag_slug = None
        if options.get("marker_tag", True):
            source_instance = db.get(models.NetboxInstance, job.source_instance_id)
            marker_tag_slug = _marker_tag_slug(source_instance)
        execute_job(
            db, job, registry=registry, target_client=target_client,
            fail_fast=bool(options.get("fail_fast", False)),
            marker_tag_slug=marker_tag_slug,
        )
    except Exception:  # noqa: BLE001 — deliberately broad: this is a background thread with no
        # caller to propagate to. The job row is left in status="running" (never set to a
        # terminal status before an unexpected exception), which is exactly what the startup
        # resume hook looks for, so the next app start (or a manual retry) picks it back up.
        import logging
        logging.getLogger(__name__).exception("Migration job %s failed unexpectedly", job_id)
    finally:
        db.close()


@router.post("/jobs/{job_id}/execute", response_model=schemas.MigrationJobSummary)
def execute_migration(
    job_id: str, payload: schemas.MigrationExecuteRequest,
    db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor")),
):
    job = db.get(models.MigrationJob, job_id)
    if job is None:
        raise HTTPException(404, "Migration job not found.")
    if job.status == "planning":
        raise HTTPException(409, "The job is still being planned and cannot be executed yet.")
    require_resource_admin(ctx, "instance", job.target_instance_id, db)
    if job.status not in ("planned",):
        raise HTTPException(409, f"Job is in status={job.status!r}; only a freshly planned job can be started this way.")
    if not payload.confirm:
        raise HTTPException(400, "Set confirm=true to start writing to the target instance.")

    ambiguous_count = job_ambiguous_count(db, job_id)
    if ambiguous_count:
        raise HTTPException(409, f"{ambiguous_count} object(s) have an ambiguous match and need manual mapping before this job can run.")

    options = json.loads(job.options_json or "{}")
    max_rps = options.get("max_requests_per_second", 4.0)
    job.status = "running"
    db.commit()

    # Build the response before the background session starts mutating the
    # same job. Besides giving callers a deterministic "running" response,
    # this avoids concurrent access to one SQLite connection in tests.
    db.refresh(job)
    summary = _job_summary(db, job)

    thread = threading.Thread(target=_run_in_background, args=(job_id, max_rps), daemon=True)
    thread.start()
    return summary


def job_ambiguous_count(db: Session, job_id: str) -> int:
    return (
        db.query(models.MigrationJobItem)
        .filter(models.MigrationJobItem.job_id == job_id, models.MigrationJobItem.planned_action == "ambiguous")
        .count()
    )


@router.post("/jobs/{job_id}/cancel", response_model=schemas.MigrationJobSummary)
def cancel_migration(
    job_id: str, db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor")),
):
    job = db.get(models.MigrationJob, job_id)
    if job is None:
        raise HTTPException(404, "Migration job not found.")
    require_resource_admin(ctx, "instance", job.target_instance_id, db)
    if job.status != "running":
        raise HTTPException(409, f"Job is in status={job.status!r}; only a running job can be cancelled.")
    job.cancel_requested = True
    db.commit()
    db.refresh(job)
    return _job_summary(db, job)


@router.post("/jobs/{job_id}/retry-failed", response_model=schemas.MigrationJobSummary)
def retry_failed(
    job_id: str, db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor")),
):
    """Resets every errored item/patch back to pending and re-runs — relies on the same idempotent, natural-key-checked execution path as a normal resume."""
    job = db.get(models.MigrationJob, job_id)
    if job is None:
        raise HTTPException(404, "Migration job not found.")
    require_resource_admin(ctx, "instance", job.target_instance_id, db)
    if job.status not in ("completed_with_errors", "failed"):
        raise HTTPException(409, f"Job is in status={job.status!r}; only a job with errors can be retried.")

    db.query(models.MigrationJobItem).filter(
        models.MigrationJobItem.job_id == job_id, models.MigrationJobItem.execution_status == "error",
    ).update({"execution_status": "pending", "error_detail": None}, synchronize_session=False)
    db.query(models.MigrationJobPatch).filter(
        models.MigrationJobPatch.job_id == job_id, models.MigrationJobPatch.execution_status == "error",
    ).update({"execution_status": "pending", "error_detail": None}, synchronize_session=False)
    job.status = "running"
    job.phase = "primary"
    job.cancel_requested = False
    db.commit()

    options = json.loads(job.options_json or "{}")
    thread = threading.Thread(target=_run_in_background, args=(job_id, options.get("max_requests_per_second", 4.0)), daemon=True)
    thread.start()

    db.refresh(job)
    return _job_summary(db, job)


@router.post("/jobs/{job_id}/rollback", response_model=schemas.MigrationRollbackResponse)
def rollback_migration(
    job_id: str, payload: schemas.MigrationExecuteRequest,
    db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor")),
):
    job = db.get(models.MigrationJob, job_id)
    if job is None:
        raise HTTPException(404, "Migration job not found.")
    require_resource_admin(ctx, "instance", job.target_instance_id, db)
    allowed = {"completed", "completed_with_errors", "cancelled", "failed", "rolling_back"}
    if job.status not in allowed:
        raise HTTPException(
            409,
            f"Job is in status={job.status!r}; rollback is allowed only after execution has stopped.",
        )
    if not payload.confirm:
        raise HTTPException(400, "Set confirm=true to delete objects created by this migration.")

    target_instance = _get_instance_or_404(db, job.target_instance_id)
    options = json.loads(job.options_json or "{}")
    target_client = build_client(
        target_instance.base_url, crypto.decrypt(target_instance.api_token_encrypted),
        target_instance.verify_ssl, read_only=False,
        max_requests_per_second=options.get("max_requests_per_second", 4.0),
    )
    rollback_job(db, job, registry=registry_mod.load_registry(), target_client=target_client)
    db.refresh(job)

    deleted = db.query(models.MigrationJobItem).filter_by(
        job_id=job.id, planned_action="create", execution_status="rolled_back",
    ).count()
    failed = db.query(models.MigrationJobItem).filter_by(
        job_id=job.id, planned_action="create", execution_status="rollback_error",
    ).count()
    untouched_mapped = db.query(models.MigrationJobItem).filter_by(
        job_id=job.id, planned_action="map",
    ).count()
    untouched_updated = db.query(models.MigrationJobItem).filter_by(
        job_id=job.id, planned_action="update",
    ).count()
    return schemas.MigrationRollbackResponse(
        job=_job_summary(db, job),
        detail=(
            "Best-effort rollback deleted only objects created by this job. "
            "Mapped and updated objects were not touched because their prior state was not recorded; "
            "patches on surviving objects were also left unchanged. The marker tag definition was retained."
        ),
        deleted=deleted,
        failed=failed,
        untouched_mapped=untouched_mapped,
        untouched_updated=untouched_updated,
    )


def _run_rollback_in_background(job_id: str) -> None:
    """Resume an orphaned rollback after process restart."""
    db = SessionLocal()
    try:
        job = db.get(models.MigrationJob, job_id)
        if job is None or job.status != "rolling_back":
            return
        target_instance = db.get(models.NetboxInstance, job.target_instance_id)
        options = json.loads(job.options_json or "{}")
        target_client = build_client(
            target_instance.base_url, crypto.decrypt(target_instance.api_token_encrypted),
            target_instance.verify_ssl, read_only=False,
            max_requests_per_second=options.get("max_requests_per_second", 4.0),
        )
        rollback_job(db, job, registry=registry_mod.load_registry(), target_client=target_client)
    except Exception:  # noqa: BLE001 - keep rolling_back so the next restart can resume again
        import logging
        logging.getLogger(__name__).exception("Migration rollback %s failed unexpectedly", job_id)
    finally:
        db.close()


@router.get("/jobs/{job_id}/report")
def get_report(
    job_id: str, format: str = "html", download: bool = False,
    db: Session = Depends(get_db), _: AccessContext = Depends(require_role("viewer")),
):
    job = db.get(models.MigrationJob, job_id)
    if job is None:
        raise HTTPException(404, "Migration job not found.")
    if job.status == "planning":
        raise HTTPException(409, "The report is not available until planning has finished.")
    # Reports expose per-object source/target IDs and links, so both instances must be visible.
    _require_job_visibility(job, _, db)
    registry = registry_mod.load_registry()
    report = build_report(db, job, registry)
    if format == "json":
        return render_json(report)
    headers = {"Content-Disposition": f'attachment; filename="migration-{job.id}.html"'} if download else None
    return Response(content=render_html(report), media_type="text/html", headers=headers)
