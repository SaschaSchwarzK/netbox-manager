from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("", response_model=list[schemas.AuditLogEntryOut])
def list_audit_log(db: Session = Depends(get_db), limit: int = 200):
    entries = (
        db.query(models.DeviceTypePushHistory)
        .order_by(models.DeviceTypePushHistory.created_at.desc())
        .limit(limit)
        .all()
    )
    out = []
    for e in entries:
        target = db.get(models.GithubTarget, e.repo_target_id)
        out.append(schemas.AuditLogEntryOut(
            id=e.id,
            created_at=e.created_at,
            action_type=e.target_type,
            target_name=e.target_name,
            repo_target_id=e.repo_target_id,
            repo_target_name=target.name if target else "(deleted target)",
            file_path=e.file_path,
            status=e.status,
            detail=e.detail,
            actor_sub=e.actor_sub,
            actor_name=e.actor_name,
            actor_email=e.actor_email,
        ))
    return out
