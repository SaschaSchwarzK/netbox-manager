from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import uuid
from urllib.parse import urljoin

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.auth import get_current_actor
from app.database import get_db
from app.module_type_schema import MODULE_COMPONENT_ENDPOINTS, ModuleType
from app.path_safety import validate_repo_path, validate_segment
from app.rbac import AccessContext, filter_scoped, get_access_context, has_role_at_least, require_role, role_for_resource
from app.routers._type_common import (
    decode_editor_image as _decode_editor_image, get_target as _get_target,
    github_error_to_http as _github_error_to_http, log_action as _log_action,
    resolve_instances as _resolve_instances, with_actor_trailer as _with_actor_trailer,
)
from app.services import diff as diff_mod
from app.services import github_repo, module_types as module_service, netbox_client

router = APIRouter(prefix="/api/repos/{target_id}/module-types", tags=["module-types"])


def _base_dir(target: models.GithubTarget) -> str:
    return github_repo.base_dir_for_pattern(target.module_path_pattern)


def _path(target: models.GithubTarget, module: ModuleType) -> str:
    try:
        path = target.module_path_pattern.format(
            manufacturer=validate_segment(module.manufacturer),
            model=validate_segment(module.model),
            slug=validate_segment(module.model),
        )
        return _validated_path(target, path)
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


def _validated_path(target: models.GithubTarget, path: str) -> str:
    try:
        return validate_repo_path(path, _base_dir(target), (".yml", ".yaml"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _preview(module: ModuleType) -> schemas.DeviceTypePreview:
    data = module.to_internal_dict()
    counts = {key: len(data.get(key) or []) for key in MODULE_COMPONENT_ENDPOINTS}
    return schemas.DeviceTypePreview(
        manufacturer=module.manufacturer, model=module.model, part_number=module.part_number,
        component_counts={key: count for key, count in counts.items() if count},
        custom_fields={},
    )


def _image_changes(images: dict[str, github_repo.BinaryRepoFile], existing: dict | None,
                   base_url: str, token: str, verify_ssl: bool) -> list[dict]:
    if existing is None:
        return []
    attachments = {
        str(item.get("name", "")).lower(): item
        for item in existing.get("_image_attachments", [])
    }
    changes = []
    for side, source_image in images.items():
        attachment = attachments.get(side)
        if not attachment or not attachment.get("image"):
            changes.append({"side": side, "status": "target_missing"})
            continue
        try:
            target = module_service.requests.get(
                urljoin(base_url.rstrip("/") + "/", attachment["image"]),
                headers={"Authorization": f"Token {token}"}, verify=verify_ssl, timeout=30,
            )
            target.raise_for_status()
            if hashlib.sha256(source_image.content).digest() != hashlib.sha256(target.content).digest():
                changes.append({"side": side, "status": "different"})
        except Exception as exc:
            changes.append({"side": side, "status": "different", "detail": f"Could not read target image: {exc}"})
    return changes


@router.get("", response_model=list[schemas.ModuleTypeSummary])
def list_module_types(target_id: str, db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted)
    try:
        files = github_repo.list_device_types(pat, target.repo, target.branch, _base_dir(target))
    except Exception as exc:
        raise _github_error_to_http(exc)
    result = []
    for file in files:
        try:
            payload = github_repo.get_file(pat, target.repo, target.branch, file.path)["payload"]
            result.append(schemas.ModuleTypeSummary(
                path=file.path, manufacturer=payload.get("manufacturer"),
                model=payload.get("model"), part_number=payload.get("part_number"),
            ))
        except Exception:
            result.append(schemas.ModuleTypeSummary(path=file.path))
    return result


@router.get("/file", response_model=schemas.ModuleTypeFileOut)
def get_module_type(target_id: str, path: str = Query(...), db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted)
    path = _validated_path(target, path)
    try:
        branch = github_repo.resolve_working_branch(pat, target.repo, target.branch, path)
        file = github_repo.get_file(pat, target.repo, branch, path)
        module = ModuleType(**file["payload"])
        images = github_repo.get_module_images(pat, target.repo, branch, path)
        open_pr = github_repo.get_open_pr(pat, target.repo, target.branch, path)
    except Exception as exc:
        raise _github_error_to_http(exc)
    return schemas.ModuleTypeFileOut(
        repo_target_id=target_id, path=path, sha=file["sha"], payload=module.to_internal_dict(),
        open_pr=open_pr, image_status={side: "present" for side in images},
    )


@router.get("/file/image-preview", response_model=schemas.ImagePreviewOut)
def editor_image_preview(target_id: str, path: str = Query(...), side: str = Query(...),
                         db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    if side not in ("front", "rear"):
        raise HTTPException(422, "side must be front or rear")
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted)
    path = _validated_path(target, path)
    branch = github_repo.resolve_working_branch(pat, target.repo, target.branch, path)
    image = github_repo.get_module_images(pat, target.repo, branch, path).get(side)
    if image is None:
        raise HTTPException(404, f"No {side} image found.")
    return schemas.ImagePreviewOut(side=side, filename=image.path.rsplit("/", 1)[-1],
        content_type=image.content_type, content_base64=base64.b64encode(image.content).decode("ascii"))


@router.put("/file/image", response_model=schemas.SaveResult)
def save_editor_image(target_id: str, payload: schemas.ImageFileRequest, request: Request,
                      path: str = Query(...), db: Session = Depends(get_db),
                      ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted); actor = get_current_actor(request)
    path = _validated_path(target, path)
    branch = github_repo.resolve_working_branch(pat, target.repo, target.branch, path)
    ModuleType(**github_repo.get_file(pat, target.repo, branch, path)["payload"])
    content, extension = _decode_editor_image(payload)
    destination = github_repo.module_image_destination(path, payload.side, f"image.{extension}")
    existing = github_repo.get_module_images(pat, target.repo, branch, path).get(payload.side)
    message = payload.commit_message or f"Update {payload.side} image for module type {path}"
    if existing and existing.path != destination:
        github_repo.delete_binary_file(pat, target.repo, target.branch, existing.path, message,
                                       feature_source_path=path)
    result = github_repo.save_binary_file(pat, target.repo, target.branch, destination, content, message,
        pr_body=_with_actor_trailer(payload.pr_body, actor), feature_source_path=path)
    return schemas.SaveResult(**result)


@router.delete("/file/image", response_model=schemas.SaveResult)
def delete_editor_image(target_id: str, payload: schemas.DeleteImageRequest, request: Request,
                        path: str = Query(...), db: Session = Depends(get_db),
                        ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted); actor = get_current_actor(request)
    path = _validated_path(target, path)
    branch = github_repo.resolve_working_branch(pat, target.repo, target.branch, path)
    image = github_repo.get_module_images(pat, target.repo, branch, path).get(payload.side)
    if image is None:
        raise HTTPException(404, f"No {payload.side} image found.")
    message = payload.commit_message or f"Remove {payload.side} image for module type {path}"
    result = github_repo.delete_binary_file(pat, target.repo, target.branch, image.path, message,
        pr_body=_with_actor_trailer(payload.pr_body, actor), feature_source_path=path)
    return schemas.SaveResult(**result)


@router.post("", response_model=schemas.SaveResult, status_code=201)
def create_module_type(target_id: str, payload: schemas.CreateModuleTypeRequest, request: Request,
                       db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted); actor = get_current_actor(request)
    module = ModuleType(**{**payload.payload, "manufacturer": payload.manufacturer, "model": payload.model})
    path = _path(target, module)
    if github_repo.file_exists(pat, target.repo, target.branch, path):
        raise HTTPException(400, f"{path} already exists in this repo.")
    try:
        result = github_repo.save_file(
            pat, target.repo, target.branch, path, module.to_yaml_dict(),
            payload.commit_message or f"Add module-type {module.manufacturer} {module.model}",
            pr_body=_with_actor_trailer(payload.pr_body, actor),
        )
    except Exception as exc:
        raise _github_error_to_http(exc)
    _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                status="success", detail=f"Created, PR #{result['pr_number']}", actor=actor)
    return schemas.SaveResult(**result)


@router.put("/file", response_model=schemas.SaveResult)
def save_module_type(target_id: str, payload: schemas.SaveDeviceTypeRequest, request: Request, path: str = Query(...),
                     db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted); actor = get_current_actor(request)
    path = _validated_path(target, path)
    module = ModuleType(**payload.payload)
    try:
        result = github_repo.save_file(
            pat, target.repo, target.branch, path, module.to_yaml_dict(),
            payload.commit_message or f"Update module-type {module.manufacturer} {module.model}",
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
def delete_module_type(target_id: str, payload: schemas.DeleteDeviceTypeRequest, request: Request, path: str = Query(...),
                       db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); actor = get_current_actor(request)
    path = _validated_path(target, path)
    github_repo.delete_file(crypto.decrypt(target.pat_encrypted), target.repo, target.branch, path, payload.sha,
                            payload.commit_message or f"Remove module-type {path}")
    _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                status="success", detail="Deleted", actor=actor)


@router.post("/import", response_model=schemas.SaveResult, status_code=201)
def import_yaml(target_id: str, payload: schemas.ImportYamlRequest, request: Request,
                db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    try:
        module = ModuleType(**(yaml.safe_load(payload.yaml_text) or {}))
    except Exception as exc:
        raise HTTPException(422, f"YAML does not match module-type schema: {exc}")
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted); actor = get_current_actor(request)
    path = _path(target, module)
    sha = github_repo.get_file(pat, target.repo, target.branch, path)["sha"] if github_repo.file_exists(pat, target.repo, target.branch, path) else None
    result = github_repo.save_file(
        pat, target.repo, target.branch, path, module.to_yaml_dict(),
        payload.commit_message or f"Import module-type {module.manufacturer} {module.model}", sha=sha,
        pr_body=_with_actor_trailer(payload.pr_body, actor),
    )
    _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                status="success", detail=f"Imported, PR #{result['pr_number']}", actor=actor)
    return schemas.SaveResult(**result)


@router.get("/file/coverage", response_model=list[schemas.CoverageEntry])
def coverage(target_id: str, path: str = Query(...), db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted)
    path = _validated_path(target, path)
    module = ModuleType(**github_repo.get_file(pat, target.repo, target.branch, path)["payload"])
    images = github_repo.get_module_images(pat, target.repo, target.branch, path)
    instances = filter_scoped(db.query(models.NetboxInstance).all(), "instance", ctx, db)
    def check(instance):
        try:
            existing = module_service.get_existing(instance.base_url, crypto.decrypt(instance.api_token_encrypted),
                                                   netbox_client.verify_for_instance(instance), module.manufacturer, module.model)
            token = crypto.decrypt(instance.api_token_encrypted)
            changes = _image_changes(images, existing, instance.base_url, token, netbox_client.verify_for_instance(instance))
            result = diff_mod.diff_module_type(module.to_internal_dict(), existing, changes)
            return schemas.CoverageEntry(instance_id=instance.id, instance_name=instance.name,
                                         status=result["status"])
        except Exception as exc:
            return schemas.CoverageEntry(instance_id=instance.id, instance_name=instance.name, status="error", error=str(exc))
    with ThreadPoolExecutor(max_workers=8) as executor:
        return list(executor.map(check, instances))


@router.post("/bulk-import/scan", response_model=list[schemas.BulkImportScanEntry])
def bulk_import_scan(target_id: str, payload: schemas.BulkImportScanRequest, db: Session = Depends(get_db),
                     ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); source_pat = payload.source_pat or crypto.decrypt(target.pat_encrypted)
    rows = github_repo.scan_repository_paths(source_pat, payload.source_repo, payload.source_branch, payload.source_base_dir)
    return [schemas.BulkImportScanEntry(path=row["path"], manufacturer_guess=row.get("manufacturer"),
                                        model=row.get("model"), part_number=row.get("part_number")) for row in rows]


@router.post("/bulk-import/preview", response_model=schemas.DeviceTypePreview)
def bulk_import_preview(target_id: str, payload: schemas.BulkImportPreviewRequest, db: Session = Depends(get_db),
                        ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); source_pat = payload.source_pat or crypto.decrypt(target.pat_encrypted)
    preview = _preview(ModuleType(**github_repo.get_file(source_pat, payload.source_repo, payload.source_branch, payload.path)["payload"]))
    images = github_repo.get_module_images(source_pat, payload.source_repo, payload.source_branch, payload.path)
    preview.image_status = {side: "present" for side in images}
    return preview


@router.post("/bulk-import/image-preview", response_model=schemas.ImagePreviewOut)
def bulk_import_image_preview(target_id: str, payload: schemas.BulkImportImagePreviewRequest,
                              db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); source_pat = payload.source_pat or crypto.decrypt(target.pat_encrypted)
    image = github_repo.get_module_images(
        source_pat, payload.source_repo, payload.source_branch, payload.path
    ).get(payload.side)
    if image is None:
        raise HTTPException(404, f"No {payload.side} image found in the source repository.")
    return schemas.ImagePreviewOut(side=payload.side, filename=image.path.rsplit("/", 1)[-1],
        content_type=image.content_type, content_base64=base64.b64encode(image.content).decode("ascii"))


@router.post("/bulk-import", response_model=schemas.BulkImportResult)
def bulk_import(target_id: str, payload: schemas.BulkImportRequest, request: Request,
                db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted)
    source_pat = payload.source_pat or pat; actor = get_current_actor(request); files = []; failures = []
    for source_path in payload.paths:
        try:
            module = ModuleType(**github_repo.get_file(source_pat, payload.source_repo, payload.source_branch, source_path)["payload"])
            destination = _path(target, module)
            files.append({"path": destination, "payload": module.to_yaml_dict()})
            for side, image in github_repo.get_module_images(source_pat, payload.source_repo, payload.source_branch, source_path).items():
                files.append({"path": github_repo.module_image_destination(destination, side, image.path), "content": image.content})
        except Exception as exc:
            failures.append(schemas.BulkImportFailure(path=source_path, error=str(exc)))
    branch = f"bulk-import-modules/{uuid.uuid4().hex[:10]}"
    result = github_repo.bulk_create_files(pat, target.repo, target.branch, branch, files,
                                           payload.commit_message or f"Bulk import modules from {payload.source_repo}")
    failures += [schemas.BulkImportFailure(**item) for item in result["failed"]]
    pr_number = pr_url = None
    if result["created"]:
        pr = github_repo.open_bulk_pr(
            pat, target.repo, branch, target.branch,
            payload.pr_title or f"Import module types from {payload.source_repo}",
            _with_actor_trailer(payload.pr_body or "Imports module types and their images.", actor),
        ); pr_number, pr_url = pr["pr_number"], pr["pr_url"]
    return schemas.BulkImportResult(branch=branch, pr_number=pr_number, pr_url=pr_url,
                                    imported=result["created"], skipped_existing=result["skipped"], failed=failures)


def _module_from_netbox(fetched: dict) -> ModuleType:
    return ModuleType(**{key: value for key, value in fetched.items()
                         if key not in ("_image_attachments", "custom_fields")})


@router.post("/import-from-netbox/scan", response_model=list[schemas.ImportFromNetboxScanEntry])
def import_from_netbox_scan(target_id: str, payload: schemas.ImportFromNetboxScanRequest,
                            db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    _get_target(target_id, db, ctx)
    instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")
    try:
        rows = module_service.list_on_instance(instance.base_url, crypto.decrypt(instance.api_token_encrypted), netbox_client.verify_for_instance(instance))
    except Exception as exc:
        raise HTTPException(502, f"Could not list module types from {instance.name}: {exc}")
    return [schemas.ImportFromNetboxScanEntry(manufacturer=row["manufacturer"], model=row["model"],
                                               slug=row["model"], part_number=row.get("part_number")) for row in rows]


@router.post("/import-from-netbox/preview", response_model=schemas.DeviceTypePreview)
def import_from_netbox_preview(target_id: str, payload: schemas.ModuleImportFromNetboxPreviewRequest,
                               db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    _get_target(target_id, db, ctx); instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")
    fetched = module_service.get_existing(instance.base_url, crypto.decrypt(instance.api_token_encrypted),
                                          netbox_client.verify_for_instance(instance), payload.manufacturer, payload.model)
    if fetched is None:
        raise HTTPException(404, "No longer found on this instance.")
    preview = _preview(_module_from_netbox(fetched))
    preview.image_status = {
        str(item.get("name", "")).lower(): "present"
        for item in fetched.get("_image_attachments", [])
        if str(item.get("name", "")).lower() in ("front", "rear")
    }
    return preview


@router.post("/import-from-netbox/image-preview", response_model=schemas.ImagePreviewOut)
def import_from_netbox_image_preview(target_id: str, payload: schemas.ModuleImportFromNetboxImagePreviewRequest,
                                     db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    _get_target(target_id, db, ctx); instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")
    token = crypto.decrypt(instance.api_token_encrypted)
    fetched = module_service.get_existing(instance.base_url, token, netbox_client.verify_for_instance(instance),
                                          payload.manufacturer, payload.model)
    attachment = next((item for item in (fetched or {}).get("_image_attachments", [])
                       if str(item.get("name", "")).lower() == payload.side), None)
    if attachment is None:
        raise HTTPException(404, f"No {payload.side} image found on this instance.")
    image = module_service.get_attachment_file(instance.base_url, token, netbox_client.verify_for_instance(instance), attachment)
    return schemas.ImagePreviewOut(side=payload.side, filename=image.path,
        content_type=image.content_type, content_base64=base64.b64encode(image.content).decode("ascii"))


@router.post("/import-from-netbox", response_model=schemas.BulkImportResult)
def import_from_netbox(target_id: str, payload: schemas.ModuleImportFromNetboxRequest, request: Request,
                       db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")
    pat = crypto.decrypt(target.pat_encrypted); token = crypto.decrypt(instance.api_token_encrypted)
    actor = get_current_actor(request); files = []; failures = []
    for selection in payload.selections:
        key = f"{selection.manufacturer}/{selection.model}"
        try:
            fetched = module_service.get_existing(instance.base_url, token, netbox_client.verify_for_instance(instance),
                                                  selection.manufacturer, selection.model)
            if fetched is None:
                raise ValueError("No longer found on this instance.")
            module = _module_from_netbox(fetched); destination = _path(target, module)
            files.append({"path": destination, "payload": module.to_yaml_dict()})
            seen = set()
            for attachment in fetched.get("_image_attachments", []):
                side = str(attachment.get("name", "")).lower()
                if side not in ("front", "rear"):
                    continue
                if side in seen:
                    raise ValueError(f"Multiple {side} image attachments found; maximum is one")
                seen.add(side)
                image = module_service.get_attachment_file(instance.base_url, token, netbox_client.verify_for_instance(instance), attachment)
                files.append({"path": github_repo.module_image_destination(destination, side, image.path), "content": image.content})
        except Exception as exc:
            failures.append(schemas.BulkImportFailure(path=key, error=str(exc)))
    branch = f"import-modules-from-netbox/{uuid.uuid4().hex[:10]}"
    result = github_repo.bulk_create_files(pat, target.repo, target.branch, branch, files,
                                           payload.commit_message or f"Import module types from {instance.name}")
    failures += [schemas.BulkImportFailure(**item) for item in result["failed"]]
    pr_number = pr_url = None
    if result["created"]:
        pr = github_repo.open_bulk_pr(pat, target.repo, branch, target.branch,
            payload.pr_title or f"Import module types from {instance.name}",
            _with_actor_trailer(payload.pr_body or "Imports module types and images from NetBox.", actor))
        pr_number, pr_url = pr["pr_number"], pr["pr_url"]
    return schemas.BulkImportResult(branch=branch, pr_number=pr_number, pr_url=pr_url,
        imported=result["created"], skipped_existing=result["skipped"], failed=failures)


@router.post("/file/diff-with-netbox", response_model=list[schemas.InstanceDiffResult])
def diff_with_netbox(target_id: str, payload: schemas.PushToNetboxRequest, path: str = Query(...),
                     db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); path = _validated_path(target, path); module = ModuleType(**github_repo.get_file(
        crypto.decrypt(target.pat_encrypted), target.repo, target.branch, path)["payload"])
    images = github_repo.get_module_images(crypto.decrypt(target.pat_encrypted), target.repo, target.branch, path)
    results = []
    for instance in _resolve_instances(db, payload.instance_ids, payload.tags, ctx):
        if not has_role_at_least(role_for_resource(ctx, "instance", instance.id), "editor"):
            results.append(schemas.InstanceDiffResult(instance_id=instance.id, instance_name=instance.name,
                error="Blocked: editor role is required on this instance."))
            continue
        try:
            token = crypto.decrypt(instance.api_token_encrypted)
            existing = module_service.get_existing(instance.base_url, token,
                                                   netbox_client.verify_for_instance(instance), module.manufacturer, module.model)
            changes = _image_changes(images, existing, instance.base_url, token, netbox_client.verify_for_instance(instance))
            results.append(schemas.InstanceDiffResult(instance_id=instance.id, instance_name=instance.name,
                                                       diff=diff_mod.diff_module_type(module.to_internal_dict(), existing, changes)))
        except Exception as exc:
            results.append(schemas.InstanceDiffResult(instance_id=instance.id, instance_name=instance.name, error=str(exc)))
    return results


@router.post("/file/push-to-netbox", response_model=list[schemas.PushResultItem])
def push_to_netbox(target_id: str, payload: schemas.PushToNetboxRequest, request: Request, path: str = Query(...),
                   db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted); actor = get_current_actor(request)
    path = _validated_path(target, path)
    source = github_repo.get_file(pat, target.repo, target.branch, path)["payload"]
    images = github_repo.get_module_images(pat, target.repo, target.branch, path)
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
                    if not approval["found"]:
                        outcome = {"status": "error", "detail": "Blocked: no merged PR was found for the current file content."}
                    elif not approval["approved"]:
                        outcome = {"status": "error", "detail": f"Blocked: PR #{approval['pr_number']} has no approving review."}
                except Exception as exc:
                    outcome = {"status": "error", "detail": f"Could not verify PR approval (push blocked): {exc}"}
        if outcome is None:
            try:
                outcome = module_service.push(instance.base_url, crypto.decrypt(instance.api_token_encrypted),
                                              netbox_client.verify_for_instance(instance), source, payload.overwrite, images=images)
            except Exception as exc:
                outcome = {"status": "error", "detail": str(exc)}
        results.append(schemas.PushResultItem(target=instance.name, **outcome))
        _log_action(db, repo_target_id=target_id, file_path=path, target_name=instance.name,
                    status=outcome["status"], detail=outcome.get("detail"), actor=actor, action_type="netbox")
    return results
