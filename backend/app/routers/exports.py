import json
import logging
import re
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.auth import get_current_actor
from app.config import settings
from app.database import get_db
from app.rbac import AccessContext, require_role, require_visible
from app.services import netbox_client
from app.services.export import runner, schema as export_schema, storage
from app.services.migration.client import build_client
from app.timeutil import as_utc_aware, utcnow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/exports", tags=["exports"])
EXPORT_MIN_ROLE = "viewer"
FINISHED = {"completed", "failed", "cancelled", "expired"}


def _require_storage() -> None:
    if not storage.available:
        raise HTTPException(503, "Export storage is not writable. Mount a writable volume at NBM_EXPORT_DIR.")


def _owner(request: Request) -> tuple[str, dict]:
    actor = get_current_actor(request)
    owner = actor.get("sub") or ("anonymous" if settings.authentication_disabled else "")
    if not owner:
        raise HTTPException(403, "Authenticated user identity is missing.")
    return owner, actor


def _valid_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def _owned_job(job_id: str, owner: str, db: Session) -> models.ExportJob:
    if not _valid_uuid(job_id):
        raise HTTPException(404, "Export job not found.")
    job = db.get(models.ExportJob, job_id)
    if not job or job.owner_sub != owner:
        if job:
            logger.warning("Export job access denied: owner=%s job=%s", owner, job_id)
        raise HTTPException(404, "Export job not found.")
    return job


def _summary(job: models.ExportJob) -> schemas.ExportJobSummary:
    aware = lambda value: as_utc_aware(value) if value else None
    return schemas.ExportJobSummary(
        id=job.id, instance_id=job.instance_id, instance_name=job.instance_name,
        tenant_id=job.tenant_id, tenant_name=job.tenant_name, tenant_slug=job.tenant_slug,
        object_types=json.loads(job.object_types_json), fields=json.loads(job.fields_json),
        format=job.format, delimiter=job.delimiter, status=job.status,
        progress=json.loads(job.progress_json), row_counts=json.loads(job.row_counts_json), error=job.error,
        created_at=as_utc_aware(job.created_at), started_at=aware(job.started_at),
        finished_at=aware(job.finished_at), expires_at=aware(job.expires_at),
        file_name=job.file_name, file_size=job.file_size, download_name=job.download_name,
    )


@router.get("/instances/{instance_id}/schema")
def get_export_schema(
    instance_id: str, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role(EXPORT_MIN_ROLE)),
):
    _require_storage()
    require_visible("instance", instance_id, ctx, db)
    instance = db.get(models.NetboxInstance, instance_id)
    if not instance:
        raise HTTPException(404, "NetBox instance not found.")
    client = build_client(
        instance.base_url, crypto.decrypt(instance.api_token_encrypted),
        netbox_client.verify_for_instance(instance), read_only=True,
        max_requests_per_second=settings.export_max_requests_per_second,
    )
    try:
        return export_schema.cached_schema(instance_id, client)
    except Exception as exc:
        raise HTTPException(502, f"Could not load export schema from the NetBox instance: {exc}") from exc


@router.post("", response_model=schemas.ExportJobSummary, status_code=201)
def create_export(payload: schemas.ExportCreateRequest, request: Request, db: Session = Depends(get_db),
                  ctx: AccessContext = Depends(require_role(EXPORT_MIN_ROLE))):
    _require_storage()
    owner, actor = _owner(request)
    require_visible("instance", payload.instance_id, ctx, db)
    instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance:
        raise HTTPException(404, "NetBox instance not found.")
    if not payload.object_types:
        raise HTTPException(400, "Select at least one object type.")
    if payload.format not in {"csv", "xlsx"} or payload.delimiter not in {",", ";"}:
        raise HTTPException(400, "Format must be csv or xlsx and delimiter must be comma or semicolon.")
    if storage.free_mib() < settings.export_min_free_mib:
        raise HTTPException(503, f"Export storage has less than {settings.export_min_free_mib} MiB free.")
    active = db.query(models.ExportJob).filter(models.ExportJob.owner_sub == owner,
                                               models.ExportJob.status.in_({"queued", "running"})).count()
    if active >= settings.export_max_active_jobs_per_user:
        raise HTTPException(429, f"At most {settings.export_max_active_jobs_per_user} active export jobs are allowed per user.")
    client = build_client(instance.base_url, crypto.decrypt(instance.api_token_encrypted),
                          netbox_client.verify_for_instance(instance), read_only=True,
                          max_requests_per_second=settings.export_max_requests_per_second)
    try:
        live_schema = export_schema.cached_schema(instance.id, client)
        available = {item["key"]: item for item in live_schema["object_types"]}
        normalized_fields = {}
        for type_key in payload.object_types:
            if type_key not in available:
                raise HTTPException(400, f"Unknown object type: {type_key}")
            selected = payload.fields.get(type_key, schemas.ExportFieldSelection())
            optional = {item["key"] for item in available[type_key]["optional"]}
            unknown_optional = set(selected.optional) - optional
            if unknown_optional:
                raise HTTPException(400, f"Unknown optional field for {type_key}: {sorted(unknown_optional)[0]}")
            custom = {item["name"]: item for item in available[type_key]["custom_fields"]}
            unknown_custom = set(selected.custom_fields) - set(custom)
            if unknown_custom:
                raise HTTPException(400, f"Unknown custom field for {type_key}: {sorted(unknown_custom)[0]}")
            normalized_fields[type_key] = {"optional": selected.optional,
                                            "custom_fields": [custom[name] for name in selected.custom_fields]}
        tenant = client.get(client.nb.tenancy.tenants, id=payload.tenant_id)
        if not tenant:
            raise HTTPException(400, "Tenant not found on the selected instance.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Could not validate the export against the NetBox instance: {exc}") from exc
    tenant_name, tenant_slug = str(tenant.name), str(tenant.slug)
    extension = "xlsx" if payload.format == "xlsx" else ("zip" if len(payload.object_types) > 1 else "csv")
    timestamp = utcnow().strftime("%Y%m%d-%H%M%S")
    safe_base = re.sub(r"[^A-Za-z0-9._-]", "_", f"export_{instance.name}_{tenant_slug}_{timestamp}")
    job = models.ExportJob(
        instance_id=instance.id, instance_name=instance.name, owner_sub=owner,
        actor_name=actor.get("name"), actor_email=actor.get("email"), tenant_id=payload.tenant_id,
        tenant_name=tenant_name, tenant_slug=tenant_slug, object_types_json=json.dumps(payload.object_types),
        fields_json=json.dumps(normalized_fields), format=payload.format, delimiter=payload.delimiter,
        download_name=f"{safe_base}.{extension}",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    runner.submit_job(job.id)
    logger.info("Export job created: owner=%s job=%s instance=%s tenant=%s types=%s format=%s",
                owner, job.id, instance.id, payload.tenant_id, payload.object_types, payload.format)
    return _summary(job)


@router.get("", response_model=list[schemas.ExportJobSummary])
def list_exports(request: Request, db: Session = Depends(get_db),
                 _: AccessContext = Depends(require_role(EXPORT_MIN_ROLE))):
    _require_storage()
    owner, _actor = _owner(request)
    jobs = db.query(models.ExportJob).filter_by(owner_sub=owner).order_by(models.ExportJob.created_at.desc()).limit(200).all()
    return [_summary(job) for job in jobs]


@router.get("/{job_id}", response_model=schemas.ExportJobSummary)
def get_export(job_id: str, request: Request, db: Session = Depends(get_db),
               _: AccessContext = Depends(require_role(EXPORT_MIN_ROLE))):
    _require_storage()
    return _summary(_owned_job(job_id, _owner(request)[0], db))


@router.post("/{job_id}/cancel", response_model=schemas.ExportJobSummary)
def cancel_export(job_id: str, request: Request, db: Session = Depends(get_db),
                  _: AccessContext = Depends(require_role(EXPORT_MIN_ROLE))):
    _require_storage()
    job = _owned_job(job_id, _owner(request)[0], db)
    if job.status == "queued":
        job.status, job.finished_at = "cancelled", utcnow()
        job.expires_at = job.finished_at + timedelta(days=settings.export_retention_days)
    elif job.status == "running":
        job.cancel_requested = True
    else:
        raise HTTPException(409, f"Cannot cancel an export in status={job.status}.")
    db.commit(); db.refresh(job)
    return _summary(job)


@router.delete("/{job_id}", status_code=204)
def delete_export(job_id: str, request: Request, db: Session = Depends(get_db),
                  _: AccessContext = Depends(require_role(EXPORT_MIN_ROLE))):
    _require_storage()
    owner, _actor = _owner(request)
    job = _owned_job(job_id, owner, db)
    if job.status not in FINISHED:
        raise HTTPException(409, "Only finished export jobs can be deleted.")
    storage.delete_job_files(job)
    db.delete(job); db.commit()
    logger.info("Export job deleted: owner=%s job=%s", owner, job_id)
    return Response(status_code=204)


@router.get("/{job_id}/download")
def download_export(job_id: str, request: Request, db: Session = Depends(get_db),
                    _: AccessContext = Depends(require_role(EXPORT_MIN_ROLE))):
    _require_storage()
    owner, actor = _owner(request)
    job = _owned_job(job_id, owner, db)
    if job.status != "completed" or not job.expires_at or job.expires_at <= utcnow() or not job.file_name:
        raise HTTPException(404, "Export file not found.")
    ext = job.file_name.rsplit(".", 1)[-1]
    path = storage.final_path(job.id, ext)
    if not path.is_file():
        raise HTTPException(404, "Export file not found.")
    try:
        db.add(models.AuditEvent(actor_sub=actor.get("sub"), actor_name=actor.get("name"), actor_email=actor.get("email"),
                                 action="download_exports", resource_type="export", resource_id=job.id,
                                 status="success", detail="Downloaded completed export."))
        db.commit()
    except Exception:
        # Audit persistence must not make an already generated export
        # unavailable. Keep the failure visible in application logs.
        db.rollback()
        logger.exception("Could not persist download audit event for export job %s", job.id)
    logger.info("Export downloaded: owner=%s job=%s", owner, job.id)
    media = {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "zip": "application/zip"}[ext]
    return FileResponse(path, media_type=media, filename=job.download_name)
