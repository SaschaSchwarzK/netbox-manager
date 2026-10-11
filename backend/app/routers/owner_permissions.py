from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_actor
from app.database import get_db
from app.owner_permissions import (
    LEGACY_TEMPLATE_ROOT, STATE_ROOT, TEMPLATE_ROOT, OwnerManager, load_owner_policy_files,
    validate_owner_template,
)
from app.rbac import AccessContext, filter_scoped, get_access_context, require_role
from app.routers.device_types import _get_target
from app.routers.tenant_permissions import (
    _actor_label, _audit, _audit_repo_save, _handle, _instance,
    _repo, _require_instance_admin, _stats_detail,
)
from app.tenant_permissions import MembersPresentError, validate_template_name

router = APIRouter(prefix="/api/repos/{target_id}/owner-permissions", tags=["owner-permissions"])


class TemplatePayload(BaseModel):
    payload: dict[str, Any]


class SaveTemplatePayload(TemplatePayload):
    previous_name: str | None = None


class ReconcilePayload(BaseModel):
    instance_id: str
    group_ro: str
    group_rw: str
    rw_template: str
    ro_template: str | None = None


class ApplyPayload(BaseModel):
    template: str


class DecommissionPayload(BaseModel):
    instance_id: str
    force: bool = False


def _manager(instance, repo, actor):
    from app import crypto
    from app.services import netbox_client
    from app.services.tenant_permission_backends import RequestsNetBox
    relations, blocked = load_owner_policy_files()
    netbox = RequestsNetBox(instance.base_url, crypto.decrypt(instance.api_token_encrypted), netbox_client.verify_for_instance(instance))
    return OwnerManager(netbox, repo, relations, blocked, instance.name, actor)


@router.get("/templates")
def list_templates(target_id: str, db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    repo = _repo(_get_target(target_id, db, ctx))
    results = []
    paths = repo.list_paths(TEMPLATE_ROOT + "/") + repo.list_paths(LEGACY_TEMPLATE_ROOT + "/")
    seen = set()
    for path in paths:
        if path.endswith((".yaml", ".yml")) and (payload := repo.read_yaml(path)):
            if payload.get("name") in seen: continue
            seen.add(payload.get("name"))
            results.append({"name": payload.get("name"), "version": payload.get("version"), "description": payload.get("description"), "path": path})
    return sorted(results, key=lambda row: row.get("name") or "")


@router.get("/templates/{name}")
def get_template(target_id: str, name: str, db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    _handle(lambda: validate_template_name(name))
    repo = _repo(_get_target(target_id, db, ctx))
    for path in (
        f"{TEMPLATE_ROOT}/custom/{name}.yaml",
        f"{TEMPLATE_ROOT}/{name}.yaml",
        f"{LEGACY_TEMPLATE_ROOT}/custom/{name}.yaml",
        f"{LEGACY_TEMPLATE_ROOT}/{name}.yaml",
    ):
        if (payload := repo.read_yaml(path)) is not None:
            return {"path": path, "payload": payload}
    raise HTTPException(404, "Owner permission template not found.")


@router.post("/validate")
def validate(payload: TemplatePayload, target_id: str, db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    _get_target(target_id, db, ctx)
    relations, blocked = load_owner_policy_files()
    return {"valid": True, "normalized": _handle(lambda: validate_owner_template(payload.payload, relations, blocked))}


@router.put("/templates")
def save_template(target_id: str, payload: SaveTemplatePayload, request: Request, db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); repo = _repo(target)
    relations, blocked = load_owner_policy_files()
    normalized = _handle(lambda: validate_owner_template(payload.payload, relations, blocked)); name = normalized["name"]
    if payload.previous_name: _handle(lambda: validate_template_name(payload.previous_name))
    path = f"{TEMPLATE_ROOT}/custom/{name}.yaml"; changes = {path: normalized}
    previous = f"{TEMPLATE_ROOT}/custom/{payload.previous_name}.yaml" if payload.previous_name else None
    if previous and payload.previous_name != name and repo.read_yaml(previous) is not None: changes[previous] = None
    legacy_previous = f"{LEGACY_TEMPLATE_ROOT}/custom/{payload.previous_name}.yaml" if payload.previous_name else None
    if legacy_previous and repo.read_yaml(legacy_previous) is not None: changes[legacy_previous] = None
    actor = get_current_actor(request)
    try:
        result = repo.write_pr(changes, f"Update owner permission template {name}", f"Owner template update requested by {_actor_label(request)}.")
        _audit(db, target, path, "success", f"Owner template saved, PR #{result['number']}", actor, action_type="github")
        return {"path": path, "pr": result}
    except Exception as exc:
        _audit(db, target, path, "error", str(exc), actor, action_type="github")
        return _handle(lambda exc=exc: (_ for _ in ()).throw(exc))


@router.get("/managed")
def list_managed(target_id: str, db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    repo = _repo(_get_target(target_id, db, ctx)); results = []
    for path in repo.list_paths(STATE_ROOT + "/"):
        if not path.endswith("/metadata.yaml"): continue
        parts = path.split("/")
        if len(parts) != 4: continue
        instance = db.query(models.NetboxInstance).filter(models.NetboxInstance.name == parts[2]).one_or_none()
        if instance and filter_scoped([instance], "instance", ctx, db):
            results.append({"instance_id": instance.id, "instance": instance.name, "metadata": repo.read_yaml(path) or {}})
    return results


def _selected(repo, template: str, db: Session, ctx: AccessContext):
    validate_template_name(template); selected = []
    for path in repo.list_paths(STATE_ROOT + "/"):
        if not path.endswith("/metadata.yaml"): continue
        meta = repo.read_yaml(path) or {}; refs = {meta.get("rw_template", {}).get("name"), meta.get("ro_template", {}).get("name"), meta.get("ro_template", {}).get("derived_from")}
        if template not in refs: continue
        instance = db.query(models.NetboxInstance).filter(models.NetboxInstance.name == path.split("/")[2]).one_or_none()
        if instance and filter_scoped([instance], "instance", ctx, db): selected.append((instance, meta, path))
    return selected


@router.post("/reconcile")
def reconcile(target_id: str, payload: ReconcilePayload, request: Request, db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); instance = _instance(payload.instance_id, db, ctx); _require_instance_admin(instance, ctx, db)
    actor = get_current_actor(request); path = f"{STATE_ROOT}/{instance.name}"
    try:
        result = _manager(instance, _repo(target), _actor_label(request)).reconcile(payload.group_ro, payload.group_rw, payload.rw_template, payload.ro_template)
        _audit(db, target, path, "success", _stats_detail(result), actor, target_name=instance.name); _audit_repo_save(db, target, path, result, actor)
        return result
    except Exception as exc:
        _audit(db, target, path, "error", str(exc), actor, target_name=instance.name)
        return _handle(lambda exc=exc: (_ for _ in ()).throw(exc))


@router.post("/apply/plan")
def apply_plan(target_id: str, payload: ApplyPayload, db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); repo = _repo(target); selected = _handle(lambda: _selected(repo, payload.template, db, ctx))
    for instance, _, _ in selected: _require_instance_admin(instance, ctx, db)
    results = []
    for instance, meta, path in selected:
        try:
            plan = _manager(instance, repo, "plan").plan(meta["group_ro"], meta["group_rw"], meta["rw_template"]["name"], meta.get("ro_template", {}).get("name"))
            results.append({"instance": instance.name, "status": "success", "plan": plan})
        except Exception as exc: results.append({"instance": instance.name, "status": "error", "error": str(exc), "metadata_path": path})
    return {"matched": len(selected), "results": results}


@router.post("/apply")
def apply(target_id: str, payload: ApplyPayload, request: Request, db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); repo = _repo(target); selected = _handle(lambda: _selected(repo, payload.template, db, ctx))
    for instance, _, _ in selected: _require_instance_admin(instance, ctx, db)
    actor = get_current_actor(request); results = []
    for instance, meta, path in selected:
        try:
            result = _manager(instance, repo, _actor_label(request)).reconcile(meta["group_ro"], meta["group_rw"], meta["rw_template"]["name"], meta.get("ro_template", {}).get("name"))
            _audit(db, target, path, "success", _stats_detail(result), actor, target_name=instance.name); _audit_repo_save(db, target, path, result, actor)
            results.append({"instance": instance.name, "status": "success", "result": result})
        except Exception as exc:
            _audit(db, target, path, "error", str(exc), actor, target_name=instance.name); results.append({"instance": instance.name, "status": "error", "error": str(exc)})
    return {"matched": len(selected), "results": results}


@router.post("/decommission")
def decommission(target_id: str, payload: DecommissionPayload, request: Request, db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); instance = _instance(payload.instance_id, db, ctx); _require_instance_admin(instance, ctx, db)
    actor = get_current_actor(request); path = f"{STATE_ROOT}/{instance.name}"
    try:
        result = _manager(instance, _repo(target), _actor_label(request)).decommission(payload.force)
        cleanup = result["permission_cleanup"]; _audit(db, target, path, "success", f"deleted={cleanup['deleted']}, detached={cleanup['detached']}", actor, target_name=instance.name); _audit_repo_save(db, target, path, result, actor)
        return result
    except MembersPresentError as exc:
        _audit(db, target, path, "error", str(exc), actor, target_name=instance.name)
        raise HTTPException(409, {"code": "group_has_members", "message": str(exc)}) from exc
    except Exception as exc:
        _audit(db, target, path, "error", str(exc), actor, target_name=instance.name)
        return _handle(lambda exc=exc: (_ for _ in ()).throw(exc))
