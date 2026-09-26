import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db
from app.rbac import AccessContext, get_access_context, scoped_resource_ids, can_access
from app.services import drift as drift_mod

router = APIRouter(prefix="/api/drift", tags=["drift"])


def _to_out(record: models.DriftRecord, db: Session) -> schemas.DriftRecordOut:
    instance = db.get(models.NetboxInstance, record.instance_id)
    target = db.get(models.GithubTarget, record.repo_target_id)
    try:
        detail = json.loads(record.detail_json)
    except Exception:
        detail = {}
    diff_payload = detail if "status" in detail else None
    return schemas.DriftRecordOut(
        id=record.id,
        instance_id=record.instance_id,
        instance_name=instance.name if instance else "(deleted instance)",
        repo_target_id=record.repo_target_id,
        repo_target_name=target.name if target else "(deleted target)",
        file_path=record.file_path,
        status=record.status,
        diff=diff_payload,
        checked_at=record.checked_at,
    )


def _filter_visible_records(records: list[models.DriftRecord], ctx: AccessContext, db: Session) -> list[models.DriftRecord]:
    if not ctx.scoping_active:
        return records
    allowed_by_instance = scoped_resource_ids(db, "instance")
    if not allowed_by_instance:
        return records
    return [r for r in records if can_access(r.instance_id, allowed_by_instance, ctx.groups)]


@router.get("", response_model=list[schemas.DriftRecordOut])
def list_drift(db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    records = db.query(models.DriftRecord).order_by(models.DriftRecord.checked_at.desc()).all()
    records = _filter_visible_records(records, ctx, db)
    return [_to_out(r, db) for r in records]


@router.post("/check-now", response_model=list[schemas.DriftRecordOut])
def check_now(db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    drift_mod.run_full_check(db)
    records = db.query(models.DriftRecord).order_by(models.DriftRecord.checked_at.desc()).all()
    records = _filter_visible_records(records, ctx, db)
    return [_to_out(r, db) for r in records]
