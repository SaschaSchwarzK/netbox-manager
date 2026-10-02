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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.auth import get_current_actor
from app.database import SessionLocal, get_db
from app.rbac import AccessContext, filter_scoped, require_resource_admin, require_role, require_visible
from app.services.migration import registry as registry_mod
from app.services.migration.client import build_client
from app.services.migration.executor import execute_job
from app.services.migration.matcher import MappingAction, MappingOverride
from app.services.migration.mapping_skeleton import build_mapping_skeleton
from app.services.migration.planner import build_plan, persist_plan
from app.services.migration.report import build_report, render_html, render_json
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
        status=job.status, phase=job.phase,
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
    """The registry's selectable types plus a short human label, for the type-selection step of the wizard."""
    from app.services.migration.report import display_name

    registry = registry_mod.load_registry()
    return [
        {"type": key, "label": display_name(key), "dependencies": list(registry[key].dependencies),
         "optional_dependencies": list(registry[key].optional_dependencies)}
        for key in registry.selectable_types()
    ]


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

    source_instance = _get_instance_or_404(db, payload.source_instance_id)
    target_instance = _get_instance_or_404(db, payload.target_instance_id)
    try:
        _, _, version_warnings = check_migration_version_compatibility(
            source_instance.base_url, crypto.decrypt(source_instance.api_token_encrypted), source_instance.verify_ssl,
            target_instance.base_url, crypto.decrypt(target_instance.api_token_encrypted), target_instance.verify_ssl,
        )
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc

    source_client, target_client = _build_clients(
        db, payload.source_instance_id, payload.target_instance_id, payload.max_requests_per_second,
    )
    mapping_overrides = _decode_mapping_overrides(payload.mapping_overrides)

    try:
        plan = build_plan(
            registry=registry, source_client=source_client, target_client=target_client,
            selected_types=set(payload.selected_types), tenant_filter=payload.tenant_filter,
            mapping_overrides=mapping_overrides, conflict_policy=payload.conflict_policy,
            include_untenanted=payload.include_untenanted,
        )
    except registry_mod.MigrationRegistryError as exc:
        raise HTTPException(400, str(exc)) from exc
    plan.warnings.extend(version_warnings)

    if payload.job_id:
        job = db.get(models.MigrationJob, payload.job_id)
        if job is None:
            raise HTTPException(404, "Migration job not found.")
        if job.status != "planned":
            raise HTTPException(409, f"Cannot re-plan a job in status={job.status!r}; only a freshly planned job can be edited.")
    else:
        job = models.MigrationJob(
            source_instance_id=payload.source_instance_id, target_instance_id=payload.target_instance_id,
            actor_sub=actor.get("sub"), actor_name=actor.get("name"), actor_email=actor.get("email"),
            status="planned",
        )
        db.add(job)

    job.tenant_filter_json = json.dumps(payload.tenant_filter)
    job.selected_types_json = json.dumps(payload.selected_types)
    job.resolved_types_json = json.dumps(plan.resolved_types)
    job.conflict_policy_json = json.dumps(payload.conflict_policy)
    job.mapping_json = json.dumps({k: v.model_dump() for k, v in payload.mapping_overrides.items()})
    job.options_json = json.dumps({
        "include_untenanted": payload.include_untenanted, "marker_tag": payload.marker_tag,
        "fail_fast": payload.fail_fast,
        "max_requests_per_second": payload.max_requests_per_second,
    })
    job.totals_json = json.dumps(plan.totals)
    job.warnings_json = json.dumps(plan.warnings)
    job.status = "planned"
    job.phase = "primary"
    db.commit()
    db.refresh(job)

    persist_plan(db, job.id, plan)
    return _job_summary(db, job)


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
    _require_job_visibility(job, ctx, db)
    registry = registry_mod.load_registry()
    items = (
        db.query(models.MigrationJobItem)
        .filter(models.MigrationJobItem.job_id == job_id)
        .order_by(models.MigrationJobItem.order_index)
        .all()
    )
    return [schemas.MigrationMappingSkeletonRow(**row.__dict__) for row in build_mapping_skeleton(items, registry)]


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

    thread = threading.Thread(target=_run_in_background, args=(job_id, max_rps), daemon=True)
    thread.start()

    db.refresh(job)
    return _job_summary(db, job)


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


@router.get("/jobs/{job_id}/report")
def get_report(
    job_id: str, format: str = "html",
    db: Session = Depends(get_db), _: AccessContext = Depends(require_role("viewer")),
):
    job = db.get(models.MigrationJob, job_id)
    if job is None:
        raise HTTPException(404, "Migration job not found.")
    # Reports expose per-object source/target IDs and links, so both instances must be visible.
    _require_job_visibility(job, _, db)
    registry = registry_mod.load_registry()
    report = build_report(db, job, registry)
    if format == "json":
        return render_json(report)
    return Response(content=render_html(report), media_type="text/html")
