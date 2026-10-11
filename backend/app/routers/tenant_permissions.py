from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import crypto, models
from app.auth import get_current_actor
from app.database import get_db
from app.rbac import AccessContext, filter_scoped, get_access_context, has_role_at_least, require_role, require_visible, role_for_resource
from app.routers.device_types import _get_target, _log_action
from app.services import netbox_client
from app.services.tenant_permission_backends import GithubPermissionRepo, RequestsNetBox
from app.tenant_permissions import (
    LEGACY_TENANT_STATE_ROOT, LEGACY_TENANT_TEMPLATE_ROOT, TENANT_STATE_ROOT,
    TENANT_TEMPLATE_ROOT, MembersPresentError,
    TenantManager, TenantPermissionError, load_policy_files, tenant_state_base,
    validate_template, validate_template_name,
)

router = APIRouter(prefix="/api/repos/{target_id}/tenant-permissions", tags=["tenant-permissions"])


class TemplatePayload(BaseModel):
    payload: dict[str, Any]


class SaveTemplatePayload(TemplatePayload):
    previous_name: str | None = None


class OnboardPayload(BaseModel):
    instance_id: str
    tenant_id: int
    oidc_group_ro: str
    oidc_group_rw: str
    rw_template: str
    ro_template: str | None = None


class ApplyPayload(BaseModel):
    template: str


class DecommissionPayload(BaseModel):
    instance_id: str
    tenant_id: int
    force: bool = False


def _repo(target: models.GithubTarget) -> GithubPermissionRepo:
    return GithubPermissionRepo(crypto.decrypt(target.pat_encrypted), target.repo, target.branch)


def _actor_label(request: Request) -> str:
    actor = get_current_actor(request)
    label = actor.get("email") or actor.get("name") or actor.get("sub") or "anonymous (auth disabled)"
    if actor.get("username"):
        label = f"{label} (OIDC username: {actor['username']})"
    return label


def _instance(instance_id: str, db: Session, ctx: AccessContext) -> models.NetboxInstance:
    value = db.get(models.NetboxInstance, instance_id)
    if not value or not filter_scoped([value], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")
    require_visible("instance", value.id, ctx, db)
    return value


def _manager(instance, repo, actor):
    relations, blocked = load_policy_files()
    netbox = RequestsNetBox(instance.base_url, crypto.decrypt(instance.api_token_encrypted), netbox_client.verify_for_instance(instance))
    return TenantManager(netbox, repo, relations, blocked, instance.name, actor)


def _require_instance_admin(instance: models.NetboxInstance, ctx: AccessContext, db: Session) -> None:
    if not has_role_at_least(role_for_resource(ctx, "instance", instance.id), "admin"):
        raise HTTPException(403, f"Admin role is required for NetBox instance {instance.name!r}.")


def _handle(call):
    try:
        return call()
    except TenantPermissionError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


def _audit(db: Session, target, path: str, status: str, detail: str, actor: dict,
           *, action_type: str = "netbox", target_name: str | None = None) -> None:
    _log_action(db, repo_target_id=target.id, file_path=path,
                target_name=target_name or target.name, status=status, detail=detail,
                actor=actor, action_type=action_type)


def _stats_detail(result: dict[str, Any]) -> str:
    stats = result.get("stats", {})
    totals = {key: sum(role.get(key, 0) for role in stats.values()) for key in ("created", "updated", "deleted")}
    return (f"created={totals['created']}, updated={totals['updated']}, deleted={totals['deleted']}, "
            f"unscoped={len(result.get('unscoped_grants', []))}")


def _audit_repo_save(db: Session, target, path: str, result: dict[str, Any], actor: dict) -> None:
    """Tenant operations can push NetBox and save generated state to GitHub."""
    pr = result.get("pr")
    if pr:
        _audit(db, target, path, "success", f"Generated state saved, PR #{pr['number']}", actor,
               action_type="github")


def _managed_metadata_paths(repo):
    """Yield new tracking paths first, with read compatibility for legacy state."""
    found: set[tuple[str, str]] = set()
    layouts = (
        (TENANT_STATE_ROOT + "/", 2, 4, 6),
        (LEGACY_TENANT_STATE_ROOT + "/", 1, 3, 5),
    )
    for prefix, instance_index, tenant_index, length in layouts:
        for path in repo.list_paths(prefix):
            parts = path.split("/")
            if not path.endswith("/metadata.yaml") or len(parts) != length:
                continue
            key = (parts[instance_index], parts[tenant_index])
            if key in found:
                continue
            found.add(key)
            yield path, key[0], key[1]


@router.get("/templates")
def list_templates(target_id: str, db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    target = _get_target(target_id, db, ctx)
    repo = _repo(target)
    paths = [
        p for p in repo.list_paths(TENANT_TEMPLATE_ROOT + "/")
        if p.endswith((".yaml", ".yml"))
    ]
    for path in repo.list_paths(LEGACY_TENANT_TEMPLATE_ROOT + "/"):
        parts = path.split("/")
        is_legacy = len(parts) == 2 or (len(parts) == 3 and parts[1] == "custom")
        if is_legacy and path.endswith((".yaml", ".yml")):
            paths.append(path)
    results = []
    seen = set()
    for path in paths:
        payload = repo.read_yaml(path)
        name = payload.get("name") if payload else None
        if payload and name not in seen:
            seen.add(name)
            results.append({"name": payload.get("name"), "version": payload.get("version"),
                            "description": payload.get("description"), "path": path})
    return sorted(results, key=lambda row: (row.get("name") or ""))


@router.get("/templates/{name}")
def get_template(target_id: str, name: str, db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    _handle(lambda: validate_template_name(name))
    target = _get_target(target_id, db, ctx)
    repo = _repo(target)
    for path in (
        f"{TENANT_TEMPLATE_ROOT}/custom/{name}.yaml",
        f"{TENANT_TEMPLATE_ROOT}/{name}.yaml",
        f"{LEGACY_TENANT_TEMPLATE_ROOT}/custom/{name}.yaml",
        f"{LEGACY_TENANT_TEMPLATE_ROOT}/{name}.yaml",
    ):
        payload = repo.read_yaml(path)
        if payload is not None:
            return {"path": path, "payload": payload}
    raise HTTPException(404, "Permission template not found.")


@router.post("/validate")
def validate(payload: TemplatePayload, target_id: str, db: Session = Depends(get_db),
             ctx: AccessContext = Depends(require_role("editor"))):
    _get_target(target_id, db, ctx)
    relations, blocked = load_policy_files()
    return {"valid": True, "normalized": _handle(lambda: validate_template(payload.payload, relations, blocked))}


@router.put("/templates")
def save_template(target_id: str, payload: SaveTemplatePayload, request: Request,
                  db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx)
    relations, blocked = load_policy_files()
    normalized = _handle(lambda: validate_template(payload.payload, relations, blocked))
    name = normalized["name"]
    if payload.previous_name:
        _handle(lambda: validate_template_name(payload.previous_name))
    path = f"{TENANT_TEMPLATE_ROOT}/custom/{name}.yaml"
    changes: dict[str, dict[str, Any] | None] = {path: normalized}
    previous_path = f"{TENANT_TEMPLATE_ROOT}/custom/{payload.previous_name}.yaml" if payload.previous_name else None
    if previous_path and payload.previous_name != name and _repo(target).read_yaml(previous_path) is not None:
        changes[previous_path] = None
    legacy_previous = (
        f"{LEGACY_TENANT_TEMPLATE_ROOT}/custom/{payload.previous_name}.yaml"
        if payload.previous_name else None
    )
    if legacy_previous and _repo(target).read_yaml(legacy_previous) is not None:
        changes[legacy_previous] = None
    actor = get_current_actor(request)
    try:
        result = _repo(target).write_pr(changes, f"Update permission template {name}",
                                        f"Permission template update requested by {_actor_label(request)}.")
        _audit(db, target, path, "success", f"Template saved, PR #{result['number']}", actor,
               action_type="github")
    except Exception as exc:
        _audit(db, target, path, "error", str(exc), actor, action_type="github")
        return _handle(lambda exc=exc: (_ for _ in ()).throw(exc))
    return {"path": path, "pr": result}


@router.get("/tenants")
def list_managed_tenants(target_id: str, db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    target = _get_target(target_id, db, ctx)
    repo = _repo(target)
    results = []
    for path, instance_name, tenant_key in _managed_metadata_paths(repo):
        instance = db.query(models.NetboxInstance).filter(models.NetboxInstance.name == instance_name).one_or_none()
        if not instance or not filter_scoped([instance], "instance", ctx, db):
            continue
        metadata = repo.read_yaml(path) or {}
        results.append({"instance_id": instance.id, "instance": instance.name,
                        "tenant_id": metadata.get("tenant_id", int(tenant_key) if tenant_key.isdigit() else None),
                        "tenant_name": metadata.get("tenant_name", tenant_key), "metadata": metadata})
    return results


@router.get("/instances/{instance_id}/available-tenants")
def list_available_tenants(target_id: str, instance_id: str, db: Session = Depends(get_db),
                           q: str = Query(default="", max_length=100),
                           ctx: AccessContext = Depends(get_access_context)):
    _get_target(target_id, db, ctx)
    instance = _instance(instance_id, db, ctx)
    backend = RequestsNetBox(instance.base_url, crypto.decrypt(instance.api_token_encrypted), netbox_client.verify_for_instance(instance))
    return _handle(lambda: [{"id": int(row["id"]), "name": str(row["name"])}
                            for row in backend.tenants(q, limit=50)])


@router.post("/onboard")
def onboard(target_id: str, payload: OnboardPayload, request: Request, db: Session = Depends(get_db),
            ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx)
    instance = _instance(payload.instance_id, db, ctx)
    _require_instance_admin(instance, ctx, db)
    manager = _manager(instance, _repo(target), _actor_label(request))
    actor = get_current_actor(request)
    path = tenant_state_base(instance.name, payload.tenant_id)
    try:
        result = manager.onboard(payload.tenant_id, payload.oidc_group_ro, payload.oidc_group_rw,
                                 payload.rw_template, payload.ro_template)
        _audit(db, target, path, "success", _stats_detail(result), actor,
               target_name=instance.name)
        _audit_repo_save(db, target, path, result, actor)
        return result
    except Exception as exc:
        _audit(db, target, path, "error", str(exc), actor, target_name=instance.name)
        return _handle(lambda exc=exc: (_ for _ in ()).throw(exc))


def _selected_for_template(repo, template: str, db: Session, ctx: AccessContext):
    validate_template_name(template)
    selected = []
    for path, instance_name, _ in _managed_metadata_paths(repo):
        meta = repo.read_yaml(path) or {}
        refs = {meta.get("rw_template", {}).get("name"), meta.get("ro_template", {}).get("name"),
                meta.get("ro_template", {}).get("derived_from")}
        if template not in refs:
            continue
        instance = db.query(models.NetboxInstance).filter(models.NetboxInstance.name == instance_name).one_or_none()
        if instance and filter_scoped([instance], "instance", ctx, db):
            selected.append((instance, meta, path))
    return selected


@router.post("/apply/plan")
def apply_template_plan(target_id: str, payload: ApplyPayload, db: Session = Depends(get_db),
                        ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx)
    repo = _repo(target)
    selected = _handle(lambda: _selected_for_template(repo, payload.template, db, ctx))
    # Authorize the entire fleet before making even read-side NetBox plan calls.
    for instance, _, _ in selected:
        _require_instance_admin(instance, ctx, db)
    results = []
    for instance, meta, metadata_path in selected:
        try:
            tenant_id = int(meta["tenant_id"])
            plan = _manager(instance, repo, "plan").plan(
                tenant_id, meta["oidc_group_ro"], meta["oidc_group_rw"], meta["rw_template"]["name"],
                meta.get("ro_template", {}).get("name"),
            )
            results.append({"instance": instance.name, "tenant_id": tenant_id, "status": "success", "plan": plan})
        except Exception as exc:
            results.append({"instance": instance.name, "tenant_id": meta.get("tenant_id"),
                            "status": "error", "error": str(exc), "metadata_path": metadata_path})
    return {"matched": len(selected), "results": results}


@router.post("/apply")
def apply_template(target_id: str, payload: ApplyPayload, request: Request, db: Session = Depends(get_db),
                   ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx)
    repo = _repo(target)
    selected = _handle(lambda: _selected_for_template(repo, payload.template, db, ctx))
    for instance, _, _ in selected:
        _require_instance_admin(instance, ctx, db)
    results = []
    actor = get_current_actor(request)
    for instance, meta, metadata_path in selected:
        tenant_id = meta.get("tenant_id")
        try:
            if tenant_id is None:
                raise TenantPermissionError(f"{metadata_path}: metadata has no tenant_id")
            result = _manager(instance, repo, _actor_label(request)).onboard(
                tenant_id, meta["oidc_group_ro"], meta["oidc_group_rw"], meta["rw_template"]["name"],
                meta.get("ro_template", {}).get("name"),
            )
            _audit(db, target, metadata_path, "success", _stats_detail(result), actor,
                   target_name=instance.name)
            _audit_repo_save(db, target, metadata_path, result, actor)
            results.append({"instance": instance.name, "tenant_id": tenant_id, "status": "success", "result": result})
        except Exception as exc:
            _audit(db, target, metadata_path, "error", str(exc), actor,
                   target_name=instance.name)
            results.append({"instance": instance.name, "tenant_id": tenant_id, "status": "error", "error": str(exc)})
    return {"matched": len(selected), "results": results}


@router.post("/decommission")
def decommission(target_id: str, payload: DecommissionPayload, request: Request,
                 db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx)
    instance = _instance(payload.instance_id, db, ctx)
    _require_instance_admin(instance, ctx, db)
    manager = _manager(instance, _repo(target), _actor_label(request))
    actor = get_current_actor(request)
    path = tenant_state_base(instance.name, payload.tenant_id)
    try:
        result = manager.decommission(payload.tenant_id, payload.force)
        cleanup = result.get("permission_cleanup", {})
        _audit(db, target, path, "success", f"deleted={cleanup.get('deleted', 0)}, detached={cleanup.get('detached', 0)}", actor,
               target_name=instance.name)
        _audit_repo_save(db, target, path, result, actor)
        return result
    except MembersPresentError as exc:
        _audit(db, target, path, "error", str(exc), actor, target_name=instance.name)
        raise HTTPException(409, {"code": "group_has_members", "message": str(exc)}) from exc
    except Exception as exc:
        _audit(db, target, path, "error", str(exc), actor, target_name=instance.name)
        return _handle(lambda exc=exc: (_ for _ in ()).throw(exc))
