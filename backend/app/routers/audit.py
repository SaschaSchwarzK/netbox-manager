from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db
from app.rbac import AccessContext, get_access_context, sql_scope_condition

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("", response_model=list[schemas.AuditLogEntryOut])
def list_audit_log(db: Session = Depends(get_db), limit: int = Query(200, ge=1, le=1000),
                   offset: int = Query(0, ge=0),
                   ctx: AccessContext = Depends(get_access_context)):
    fetch_limit = offset + limit
    push_query = db.query(models.DeviceTypePushHistory)
    target_scope = sql_scope_condition(models.DeviceTypePushHistory.repo_target_id, "github_target", ctx)
    if target_scope is not None:
        push_query = push_query.filter(target_scope)
    entries = push_query.order_by(models.DeviceTypePushHistory.created_at.desc()).limit(fetch_limit).all()
    target_ids = {entry.repo_target_id for entry in entries if entry.repo_target_id}
    target_names = dict(db.query(models.GithubTarget.id, models.GithubTarget.name)
                        .filter(models.GithubTarget.id.in_(target_ids)).all()) if target_ids else {}
    out = []
    for e in entries:
        out.append(schemas.AuditLogEntryOut(
            id=e.id,
            created_at=e.created_at,
            action_type=e.target_type,
            target_name=e.target_name,
            repo_target_id=e.repo_target_id,
            repo_target_name=target_names.get(e.repo_target_id, "(deleted target)"),
            file_path=e.file_path,
            status=e.status,
            detail=e.detail,
            actor_sub=e.actor_sub,
            actor_name=e.actor_name,
            actor_email=e.actor_email,
        ))
    event_query = db.query(models.AuditEvent)
    instance_scope = sql_scope_condition(models.AuditEvent.resource_id, "instance", ctx)
    github_scope = sql_scope_condition(models.AuditEvent.resource_id, "github_target", ctx)
    if instance_scope is not None and github_scope is not None:
        event_query = event_query.filter(or_(
            ~models.AuditEvent.resource_type.in_(["instance", "github_target"]),
            and_(models.AuditEvent.resource_type == "instance", instance_scope),
            and_(models.AuditEvent.resource_type == "github_target", github_scope),
        ))
    for event in event_query.order_by(models.AuditEvent.created_at.desc()).limit(fetch_limit).all():
        out.append(schemas.AuditLogEntryOut(
            id=event.id, created_at=event.created_at, action_type=event.action,
            target_name=event.resource_id or event.resource_type, repo_target_id=None,
            repo_target_name="", file_path="", status=event.status, detail=event.detail,
            actor_sub=event.actor_sub, actor_name=event.actor_name, actor_email=event.actor_email,
            resource_type=event.resource_type, resource_id=event.resource_id,
        ))
    return sorted(out, key=lambda entry: entry.created_at, reverse=True)[offset:offset + limit]
