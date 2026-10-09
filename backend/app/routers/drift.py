import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db
from app.rbac import AccessContext, get_access_context, sql_scope_condition
from app.services import drift as drift_mod

router = APIRouter(prefix="/api/drift", tags=["drift"])


def _to_out(record: models.DriftRecord, instance_names: dict[str, str],
            target_names: dict[str, str]) -> schemas.DriftRecordOut:
    try:
        detail = json.loads(record.detail_json)
    except Exception:
        detail = {}
    diff_payload = detail if "status" in detail else None
    meta = detail.get("_drift_meta", {})
    return schemas.DriftRecordOut(
        id=record.id,
        instance_id=record.instance_id,
        instance_name=instance_names.get(record.instance_id, "(deleted instance)"),
        repo_target_id=record.repo_target_id,
        repo_target_name=target_names.get(record.repo_target_id, "(deleted target)"),
        kind=record.kind,
        file_path=record.file_path,
        status=record.status,
        diff=diff_payload,
        checked_at=record.checked_at,
        last_full_check=meta.get("last_full_check"),
        reused=meta.get("reused"),
        checks_since_full=meta.get("checks_since_full"),
    )


def _list_records(db: Session, ctx: AccessContext, limit: int, offset: int):
    query = db.query(models.DriftRecord)
    condition = sql_scope_condition(models.DriftRecord.instance_id, "instance", ctx)
    if condition is not None:
        query = query.filter(condition)
    records = query.order_by(models.DriftRecord.checked_at.desc()).offset(offset).limit(limit).all()
    instance_ids = {record.instance_id for record in records}
    target_ids = {record.repo_target_id for record in records}
    instance_names = dict(db.query(models.NetboxInstance.id, models.NetboxInstance.name)
                          .filter(models.NetboxInstance.id.in_(instance_ids)).all()) if instance_ids else {}
    target_names = dict(db.query(models.GithubTarget.id, models.GithubTarget.name)
                        .filter(models.GithubTarget.id.in_(target_ids)).all()) if target_ids else {}
    return [_to_out(record, instance_names, target_names) for record in records]


@router.get("", response_model=list[schemas.DriftRecordOut])
def list_drift(limit: int = Query(500, ge=1, le=5000), offset: int = Query(0, ge=0),
               db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    return _list_records(db, ctx, limit, offset)


@router.post("/check-now", response_model=list[schemas.DriftRecordOut])
def check_now(limit: int = Query(500, ge=1, le=5000), offset: int = Query(0, ge=0),
              db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    drift_mod.run_full_check(db)
    return _list_records(db, ctx, limit, offset)
