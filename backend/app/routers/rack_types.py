from concurrent.futures import ThreadPoolExecutor
import uuid

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.auth import get_current_actor
from app.database import get_db
from app.rack_type_schema import RackType
from app.path_safety import validate_repo_path, validate_segment
from app.rbac import AccessContext, filter_scoped, get_access_context, has_role_at_least, require_role, role_for_resource
from app.routers._type_common import (
    get_target as _get_target, github_error_to_http as _github_error_to_http,
    log_action as _log_action, resolve_instances as _resolve_instances,
    with_actor_trailer as _with_actor_trailer,
)
from app.services import diff as diff_mod
from app.services import github_repo, rack_types as rack_service, netbox_client

router = APIRouter(prefix="/api/repos/{target_id}/rack-types", tags=["rack-types"])


def _base_dir(target):
    return github_repo.base_dir_for_pattern(target.rack_path_pattern)


def _path(target, rack: RackType):
    try:
        path = target.rack_path_pattern.format(
            manufacturer=validate_segment(rack.manufacturer),
            model=validate_segment(rack.model), slug=validate_segment(rack.slug),
        )
        return _validated_path(target, path)
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


def _validated_path(target, path: str) -> str:
    try:
        return validate_repo_path(path, _base_dir(target), (".yml", ".yaml"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("", response_model=list[schemas.RackTypeSummary])
def list_rack_types(target_id: str, db: Session = Depends(get_db),
                    ctx: AccessContext = Depends(get_access_context)):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted)
    try:
        files = github_repo.list_device_types(pat, target.repo, target.branch, _base_dir(target))
        result = []
        for file in files:
            try:
                data = github_repo.get_file(pat, target.repo, target.branch, file.path)["payload"]
                result.append(schemas.RackTypeSummary(
                    path=file.path, manufacturer=data.get("manufacturer"), model=data.get("model"),
                    slug=data.get("slug"), u_height=data.get("u_height"),
                ))
            except Exception:
                result.append(schemas.RackTypeSummary(path=file.path))
        return result
    except Exception as exc:
        raise _github_error_to_http(exc)


@router.get("/file", response_model=schemas.RackTypeFileOut)
def get_rack_type(target_id: str, path: str = Query(...), db: Session = Depends(get_db),
                  ctx: AccessContext = Depends(get_access_context)):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted)
    path = _validated_path(target, path)
    try:
        branch = github_repo.resolve_working_branch(pat, target.repo, target.branch, path)
        file = github_repo.get_file(pat, target.repo, branch, path)
        rack = RackType(**file["payload"])
        open_pr = github_repo.get_open_pr(pat, target.repo, target.branch, path)
    except Exception as exc:
        raise _github_error_to_http(exc)
    return schemas.RackTypeFileOut(repo_target_id=target_id, path=path, sha=file["sha"],
                                   payload=rack.to_internal_dict(), open_pr=open_pr)


@router.post("", response_model=schemas.SaveResult, status_code=201)
def create_rack_type(target_id: str, payload: schemas.CreateRackTypeRequest, request: Request,
                     db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted); actor = get_current_actor(request)
    rack = RackType(**{**payload.payload, "manufacturer": payload.manufacturer,
                       "model": payload.model, "slug": payload.slug})
    path = _path(target, rack)
    if github_repo.file_exists(pat, target.repo, target.branch, path):
        raise HTTPException(400, f"{path} already exists in this repo.")
    try:
        result = github_repo.save_file(
            pat, target.repo, target.branch, path, rack.to_yaml_dict(),
            payload.commit_message or f"Add rack-type {rack.manufacturer} {rack.model}",
            pr_body=_with_actor_trailer(payload.pr_body, actor),
        )
    except Exception as exc:
        raise _github_error_to_http(exc)
    _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                status="success", detail=f"Created, PR #{result['pr_number']}", actor=actor)
    return schemas.SaveResult(**result)


@router.put("/file", response_model=schemas.SaveResult)
def save_rack_type(target_id: str, payload: schemas.SaveDeviceTypeRequest, request: Request,
                   path: str = Query(...), db: Session = Depends(get_db),
                   ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted); actor = get_current_actor(request)
    path = _validated_path(target, path)
    rack = RackType(**payload.payload)
    try:
        result = github_repo.save_file(
            pat, target.repo, target.branch, path, rack.to_yaml_dict(),
            payload.commit_message or f"Update rack-type {rack.manufacturer} {rack.model}",
            sha=payload.sha, pr_body=_with_actor_trailer(payload.pr_body, actor),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        raise _github_error_to_http(exc)
    _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                status="success", detail=f"Updated, PR #{result['pr_number']}", actor=actor)
    return schemas.SaveResult(**result)


@router.delete("/file", status_code=204)
def delete_rack_type(target_id: str, payload: schemas.DeleteDeviceTypeRequest, request: Request,
                     path: str = Query(...), db: Session = Depends(get_db),
                     ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); actor = get_current_actor(request)
    path = _validated_path(target, path)
    github_repo.delete_file(crypto.decrypt(target.pat_encrypted), target.repo, target.branch, path,
                            payload.sha, payload.commit_message or f"Remove rack-type {path}")
    _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                status="success", detail="Deleted", actor=actor)


@router.post("/import", response_model=schemas.SaveResult, status_code=201)
def import_yaml(target_id: str, payload: schemas.ImportYamlRequest, request: Request,
                db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    try:
        rack = RackType(**(yaml.safe_load(payload.yaml_text) or {}))
    except Exception as exc:
        raise HTTPException(422, f"YAML does not match rack-type schema: {exc}")
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted); actor = get_current_actor(request)
    path = _path(target, rack)
    sha = github_repo.get_file(pat, target.repo, target.branch, path)["sha"] if github_repo.file_exists(pat, target.repo, target.branch, path) else None
    result = github_repo.save_file(
        pat, target.repo, target.branch, path, rack.to_yaml_dict(),
        payload.commit_message or f"Import rack-type {rack.manufacturer} {rack.model}", sha=sha,
        pr_body=_with_actor_trailer(payload.pr_body, actor),
    )
    return schemas.SaveResult(**result)


@router.get("/file/coverage", response_model=list[schemas.CoverageEntry])
def coverage(target_id: str, path: str = Query(...), db: Session = Depends(get_db),
             ctx: AccessContext = Depends(get_access_context)):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted)
    path = _validated_path(target, path)
    rack = RackType(**github_repo.get_file(pat, target.repo, target.branch, path)["payload"])
    instances = filter_scoped(db.query(models.NetboxInstance).all(), "instance", ctx, db)
    def check(instance):
        try:
            existing = rack_service.get_existing(instance.base_url, crypto.decrypt(instance.api_token_encrypted),
                                                 netbox_client.verify_for_instance(instance), rack.manufacturer, rack.slug)
            status = diff_mod.diff_rack_type(rack.to_internal_dict(), existing)["status"]
            return schemas.CoverageEntry(instance_id=instance.id, instance_name=instance.name, status=status)
        except Exception as exc:
            return schemas.CoverageEntry(instance_id=instance.id, instance_name=instance.name, status="error", error=str(exc))
    with ThreadPoolExecutor(max_workers=8) as executor:
        return list(executor.map(check, instances))


@router.post("/bulk-import/scan", response_model=list[schemas.BulkImportScanEntry])
def bulk_import_scan(target_id: str, payload: schemas.RackBulkImportScanRequest, db: Session = Depends(get_db),
                     ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); source_pat = payload.source_pat or crypto.decrypt(target.pat_encrypted)
    rows = github_repo.scan_repository_paths(source_pat, payload.source_repo, payload.source_branch, payload.source_base_dir)
    return [schemas.BulkImportScanEntry(path=row["path"], manufacturer_guess=row.get("manufacturer"),
                                        slug_guess=row.get("slug"), model=row.get("model")) for row in rows]


@router.post("/bulk-import/preview", response_model=schemas.DeviceTypePreview)
def bulk_import_preview(target_id: str, payload: schemas.BulkImportPreviewRequest, db: Session = Depends(get_db),
                        ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); source_pat = payload.source_pat or crypto.decrypt(target.pat_encrypted)
    rack = RackType(**github_repo.get_file(source_pat, payload.source_repo, payload.source_branch, payload.path)["payload"])
    return schemas.DeviceTypePreview(manufacturer=rack.manufacturer, model=rack.model, slug=rack.slug,
                                     component_counts={}, custom_fields={})


@router.post("/bulk-import", response_model=schemas.BulkImportResult)
def bulk_import(target_id: str, payload: schemas.BulkImportRequest, request: Request,
                db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted)
    source_pat = payload.source_pat or pat; actor = get_current_actor(request); files = []; failures = []
    for source_path in payload.paths:
        try:
            rack = RackType(**github_repo.get_file(source_pat, payload.source_repo, payload.source_branch, source_path)["payload"])
            files.append({"path": _path(target, rack), "payload": rack.to_yaml_dict()})
        except Exception as exc:
            failures.append(schemas.BulkImportFailure(path=source_path, error=str(exc)))
    branch = f"bulk-import-racks/{uuid.uuid4().hex[:10]}"
    result = github_repo.bulk_create_files(pat, target.repo, target.branch, branch, files,
                                           payload.commit_message or f"Bulk import rack types from {payload.source_repo}")
    failures += [schemas.BulkImportFailure(**item) for item in result["failed"]]
    pr_number = pr_url = None
    if result["created"]:
        pr = github_repo.open_bulk_pr(
            pat, target.repo, branch, target.branch,
            payload.pr_title or f"Import rack types from {payload.source_repo}",
            _with_actor_trailer(payload.pr_body or "Imports rack type definitions.", actor),
        ); pr_number, pr_url = pr["pr_number"], pr["pr_url"]
    return schemas.BulkImportResult(branch=branch, pr_number=pr_number, pr_url=pr_url,
                                    imported=result["created"], skipped_existing=result["skipped"], failed=failures)


@router.post("/import-from-netbox/scan", response_model=list[schemas.ImportFromNetboxScanEntry])
def import_from_netbox_scan(target_id: str, payload: schemas.ImportFromNetboxScanRequest,
                            db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    _get_target(target_id, db, ctx); instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")
    try:
        rows = rack_service.list_on_instance(instance.base_url, crypto.decrypt(instance.api_token_encrypted), netbox_client.verify_for_instance(instance))
    except Exception as exc:
        raise HTTPException(502, f"Could not list rack types from {instance.name}: {exc}")
    return [schemas.ImportFromNetboxScanEntry(manufacturer=row["manufacturer"], model=row["model"], slug=row["slug"]) for row in rows]


@router.post("/import-from-netbox/preview", response_model=schemas.DeviceTypePreview)
def import_from_netbox_preview(target_id: str, payload: schemas.RackImportFromNetboxPreviewRequest,
                               db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    _get_target(target_id, db, ctx); instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")
    fetched = rack_service.get_existing(instance.base_url, crypto.decrypt(instance.api_token_encrypted),
                                        netbox_client.verify_for_instance(instance), payload.manufacturer, payload.slug)
    if fetched is None:
        raise HTTPException(404, "No longer found on this instance.")
    rack = RackType(**fetched)
    return schemas.DeviceTypePreview(manufacturer=rack.manufacturer, model=rack.model, slug=rack.slug,
                                     component_counts={}, custom_fields={})


@router.post("/import-from-netbox", response_model=schemas.BulkImportResult)
def import_from_netbox(target_id: str, payload: schemas.RackImportFromNetboxRequest, request: Request,
                       db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")
    pat = crypto.decrypt(target.pat_encrypted); token = crypto.decrypt(instance.api_token_encrypted)
    actor = get_current_actor(request); files = []; failures = []
    for selection in payload.selections:
        key = f"{selection.manufacturer}/{selection.slug}"
        try:
            fetched = rack_service.get_existing(instance.base_url, token, netbox_client.verify_for_instance(instance),
                                                selection.manufacturer, selection.slug)
            if fetched is None:
                raise ValueError("No longer found on this instance.")
            rack = RackType(**fetched)
            files.append({"path": _path(target, rack), "payload": rack.to_yaml_dict()})
        except Exception as exc:
            failures.append(schemas.BulkImportFailure(path=key, error=str(exc)))
    branch = f"import-racks-from-netbox/{uuid.uuid4().hex[:10]}"
    result = github_repo.bulk_create_files(pat, target.repo, target.branch, branch, files,
                                           payload.commit_message or f"Import rack types from {instance.name}")
    failures += [schemas.BulkImportFailure(**item) for item in result["failed"]]
    pr_number = pr_url = None
    if result["created"]:
        pr = github_repo.open_bulk_pr(pat, target.repo, branch, target.branch,
            payload.pr_title or f"Import rack types from {instance.name}",
            _with_actor_trailer(payload.pr_body or "Imports rack types from NetBox.", actor))
        pr_number, pr_url = pr["pr_number"], pr["pr_url"]
    return schemas.BulkImportResult(branch=branch, pr_number=pr_number, pr_url=pr_url,
        imported=result["created"], skipped_existing=result["skipped"], failed=failures)


@router.post("/file/diff-with-netbox", response_model=list[schemas.InstanceDiffResult])
def diff_with_netbox(target_id: str, payload: schemas.PushToNetboxRequest, path: str = Query(...),
                     db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); path = _validated_path(target, path); rack = RackType(**github_repo.get_file(
        crypto.decrypt(target.pat_encrypted), target.repo, target.branch, path)["payload"])
    results = []
    for instance in _resolve_instances(db, payload.instance_ids, payload.tags, ctx):
        if not has_role_at_least(role_for_resource(ctx, "instance", instance.id), "editor"):
            results.append(schemas.InstanceDiffResult(instance_id=instance.id, instance_name=instance.name,
                error="Blocked: editor role is required on this instance."))
            continue
        try:
            existing = rack_service.get_existing(instance.base_url, crypto.decrypt(instance.api_token_encrypted),
                                                 netbox_client.verify_for_instance(instance), rack.manufacturer, rack.slug)
            results.append(schemas.InstanceDiffResult(instance_id=instance.id, instance_name=instance.name,
                                                       diff=diff_mod.diff_rack_type(rack.to_internal_dict(), existing)))
        except Exception as exc:
            results.append(schemas.InstanceDiffResult(instance_id=instance.id, instance_name=instance.name, error=str(exc)))
    return results


@router.post("/file/push-to-netbox", response_model=list[schemas.PushResultItem])
def push_to_netbox(target_id: str, payload: schemas.PushToNetboxRequest, request: Request,
                   path: str = Query(...), db: Session = Depends(get_db),
                   ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted); actor = get_current_actor(request)
    path = _validated_path(target, path)
    source = github_repo.get_file(pat, target.repo, target.branch, path)["payload"]
    instances = _resolve_instances(db, payload.instance_ids, payload.tags, ctx)
    if not instances:
        raise HTTPException(400, "No matching NetBox instances (check instance_ids/tags).")
    results = []
    for instance in instances:
        outcome = None if has_role_at_least(role_for_resource(ctx, "instance", instance.id), "editor") else {
            "status": "error", "detail": "Blocked: editor role is required on this instance."
        }
        if instance.requires_approved_pr:
            if not has_role_at_least(role_for_resource(ctx, "instance", instance.id), "admin"):
                outcome = {"status": "error", "detail": "Blocked: this instance requires the admin role and an approved, merged PR."}
            else:
                try:
                    approval = github_repo.get_merged_pr_approval(pat, target.repo, target.branch, path)
                    if not approval["found"] or not approval["approved"]:
                        outcome = {"status": "error", "detail": "Blocked: no approved, merged PR was found for the current file."}
                except Exception as exc:
                    outcome = {"status": "error", "detail": f"Could not verify PR approval (push blocked): {exc}"}
        if outcome is None:
            try:
                outcome = rack_service.push(instance.base_url, crypto.decrypt(instance.api_token_encrypted),
                                            netbox_client.verify_for_instance(instance), source, payload.overwrite)
            except Exception as exc:
                outcome = {"status": "error", "detail": str(exc)}
        results.append(schemas.PushResultItem(target=instance.name, **outcome))
        _log_action(db, repo_target_id=target_id, file_path=path, target_name=instance.name,
                    status=outcome["status"], detail=outcome.get("detail"), actor=actor, action_type="netbox")
    return results
