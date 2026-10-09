from concurrent.futures import ThreadPoolExecutor, as_completed
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
from app.devicetype_schema import COMPONENT_ENDPOINTS, DeviceType
from app.routers._type_common import (
    decode_editor_image as _decode_editor_image, get_target as _get_target,
    github_error_to_http as _github_error_to_http, log_action as _log_action,
    resolve_instances as _resolve_instances, with_actor_trailer as _with_actor_trailer,
)
from app.path_safety import validate_repo_path, validate_segment
from app.rbac import AccessContext, filter_scoped, get_access_context, has_role_at_least, require_role, role_for_resource
from app.services import diff as diff_mod
from app.services import github_repo, ndx_client, netbox_client

router = APIRouter(prefix="/api/repos/{target_id}/device-types", tags=["device-types"])

def _base_dir(target: models.GithubTarget) -> str:
    return github_repo.base_dir_for_pattern(target.path_pattern)


def _validated_path(target: models.GithubTarget, path: str) -> str:
    try:
        return validate_repo_path(path, _base_dir(target), (".yml", ".yaml"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _format_path(target: models.GithubTarget, *, manufacturer: str, model: str, slug: str) -> str:
    try:
        values = {name: validate_segment(value) for name, value in {
            "manufacturer": manufacturer, "model": model, "slug": slug,
        }.items()}
        return _validated_path(target, target.path_pattern.format(**values))
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


def _to_preview(validated: DeviceType) -> schemas.DeviceTypePreview:
    """Shared by both bulk-import preview endpoints (GitHub-library and NetBox-instance sources)."""
    data = validated.to_yaml_dict()
    component_counts = {key: len(data.get(key) or []) for key in COMPONENT_ENDPOINTS}
    component_counts = {k: v for k, v in component_counts.items() if v}
    return schemas.DeviceTypePreview(
        manufacturer=validated.manufacturer, model=validated.model, slug=validated.slug, part_number=validated.part_number,
        component_counts=component_counts, custom_fields=validated.custom_fields,
    )


def _load_elevation_images(
    pat: str, target: models.GithubTarget, path: str, source: dict, branch: str | None = None,
) -> tuple[dict[str, github_repo.BinaryRepoFile], list[str]]:
    images = {}
    warnings = []
    for side in ("front", "rear"):
        try:
            image = github_repo.get_elevation_image(
                pat, target.repo, branch or target.branch, path, source["slug"], side
            )
        except Exception as exc:
            warnings.append(f"Could not read expected {side} image: {exc}")
            continue
        if image is None and source.get(f"{side}_image"):
            warnings.append(f"{side.title()} image is declared but missing from the repository")
        elif image is not None:
            images[side] = image
    return images, warnings


def _image_changes(
    source: dict, source_images: dict[str, github_repo.BinaryRepoFile], source_warnings: list[str],
    existing: dict | None, base_url: str, token: str, verify_ssl: bool,
) -> list[dict]:
    if existing is None:
        return []
    changes = []
    for side in ("front", "rear"):
        if not source.get(f"{side}_image"):
            continue
        image = source_images.get(side)
        if image is None:
            detail = next((w for w in source_warnings if w.lower().startswith(side)), None)
            changes.append({"side": side, "status": "source_missing", "detail": detail})
            continue
        target_url = (existing.get("_image_urls") or {}).get(side)
        if not target_url:
            changes.append({"side": side, "status": "target_missing"})
            continue
        try:
            target_content = netbox_client.get_image_bytes(
                str(target_url), token, verify_ssl, base_url
            )
        except Exception as exc:
            changes.append({"side": side, "status": "different", "detail": f"Could not read target image: {exc}"})
            continue
        if hashlib.sha256(image.content).digest() != hashlib.sha256(target_content).digest():
            changes.append({"side": side, "status": "different"})
    return changes


@router.get("", response_model=list[schemas.DeviceTypeSummary])
def list_device_types(target_id: str, db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    target = _get_target(target_id, db, ctx)
    pat = crypto.decrypt(target.pat_encrypted)
    try:
        files = github_repo.list_device_types(pat, target.repo, target.branch, _base_dir(target))
    except Exception as exc:
        raise _github_error_to_http(exc)

    summaries = []
    for f in files:
        try:
            content = github_repo.get_file(pat, target.repo, target.branch, f.path)
            payload = content["payload"] or {}
            summaries.append(schemas.DeviceTypeSummary(
                path=f.path,
                manufacturer=str(payload["manufacturer"]) if payload.get("manufacturer") is not None else None,
                model=str(payload["model"]) if payload.get("model") is not None else None,
                slug=str(payload["slug"]) if payload.get("slug") is not None else None,
                part_number=str(payload["part_number"]) if payload.get("part_number") is not None else None,
            ))
        except Exception:
            # Skip files that aren't parseable YAML device-types rather than failing the whole list.
            summaries.append(schemas.DeviceTypeSummary(path=f.path))
    return summaries


@router.get("/file", response_model=schemas.DeviceTypeFileOut)
def get_device_type(target_id: str, path: str = Query(...), db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    target = _get_target(target_id, db, ctx)
    path = _validated_path(target, path)
    pat = crypto.decrypt(target.pat_encrypted)
    try:
        working_branch = github_repo.resolve_working_branch(pat, target.repo, target.branch, path)
        result = github_repo.get_file(pat, target.repo, working_branch, path)
        open_pr = github_repo.get_open_pr(pat, target.repo, target.branch, path)
    except Exception as exc:
        raise _github_error_to_http(exc)
    try:
        normalized = DeviceType(**result["payload"]).to_internal_dict()
    except Exception as exc:
        raise HTTPException(422, f"Repository device-type YAML is invalid: {exc}")
    images, _ = _load_elevation_images(pat, target, path, normalized, working_branch)
    for side in images:
        normalized[f"{side}_image"] = True
    image_status = {
        side: "present" if side in images else "missing"
        for side in ("front", "rear") if normalized.get(f"{side}_image")
    }
    return schemas.DeviceTypeFileOut(
        repo_target_id=target_id, path=path, sha=result["sha"], payload=normalized,
        open_pr=open_pr, image_status=image_status,
    )


@router.get("/file/image-preview", response_model=schemas.ImagePreviewOut)
def editor_image_preview(target_id: str, path: str = Query(...), side: str = Query(...),
                         db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    if side not in ("front", "rear"):
        raise HTTPException(422, "side must be front or rear")
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted)
    path = _validated_path(target, path)
    branch = github_repo.resolve_working_branch(pat, target.repo, target.branch, path)
    source = DeviceType(**github_repo.get_file(pat, target.repo, branch, path)["payload"])
    image = github_repo.get_elevation_image(pat, target.repo, branch, path, source.slug, side)
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
    file = github_repo.get_file(pat, target.repo, branch, path); device = DeviceType(**file["payload"])
    content, extension = _decode_editor_image(payload)
    destination = github_repo.elevation_image_destination(path, device.slug, payload.side, f"image.{extension}")
    existing = github_repo.get_elevation_image(pat, target.repo, branch, path, device.slug, payload.side)
    message = payload.commit_message or f"Update {payload.side} image for {device.manufacturer} {device.model}"
    if existing and existing.path != destination:
        github_repo.delete_binary_file(pat, target.repo, target.branch, existing.path, message,
                                       feature_source_path=path)
    result = github_repo.save_binary_file(pat, target.repo, target.branch, destination, content, message,
        pr_body=_with_actor_trailer(payload.pr_body, actor), feature_source_path=path)
    updated = device.to_internal_dict(); updated[f"{payload.side}_image"] = True
    github_repo.save_file(pat, target.repo, target.branch, path, DeviceType(**updated).to_yaml_dict(), message,
                          sha=file["sha"], pr_body=_with_actor_trailer(payload.pr_body, actor))
    return schemas.SaveResult(**result)


@router.delete("/file/image", response_model=schemas.SaveResult)
def delete_editor_image(target_id: str, payload: schemas.DeleteImageRequest, request: Request,
                        path: str = Query(...), db: Session = Depends(get_db),
                        ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); pat = crypto.decrypt(target.pat_encrypted); actor = get_current_actor(request)
    path = _validated_path(target, path)
    branch = github_repo.resolve_working_branch(pat, target.repo, target.branch, path)
    file = github_repo.get_file(pat, target.repo, branch, path); device = DeviceType(**file["payload"])
    image = github_repo.get_elevation_image(pat, target.repo, branch, path, device.slug, payload.side)
    if image is None:
        raise HTTPException(404, f"No {payload.side} image found.")
    message = payload.commit_message or f"Remove {payload.side} image for {device.manufacturer} {device.model}"
    result = github_repo.delete_binary_file(pat, target.repo, target.branch, image.path, message,
        pr_body=_with_actor_trailer(payload.pr_body, actor), feature_source_path=path)
    updated = device.to_internal_dict(); updated.pop(f"{payload.side}_image", None)
    github_repo.save_file(pat, target.repo, target.branch, path, DeviceType(**updated).to_yaml_dict(), message,
                          sha=file["sha"], pr_body=_with_actor_trailer(payload.pr_body, actor))
    return schemas.SaveResult(**result)


@router.post("", response_model=schemas.SaveResult, status_code=201)
def create_device_type(
    target_id: str, payload: schemas.CreateDeviceTypeRequest, request: Request, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    target = _get_target(target_id, db, ctx)
    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)

    validated = DeviceType(**{**payload.payload, "manufacturer": payload.manufacturer,
                              "model": payload.model, "slug": payload.slug})
    path = _format_path(target, manufacturer=payload.manufacturer, slug=payload.slug, model=payload.model)

    try:
        exists = github_repo.file_exists(pat, target.repo, target.branch, path)
    except Exception as exc:
        _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                    status="error", detail=str(exc), actor=actor)
        raise _github_error_to_http(exc)
    if exists:
        _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                    status="error", detail=f"{path} already exists in this repo.", actor=actor)
        raise HTTPException(400, f"{path} already exists in this repo.")

    commit_message = payload.commit_message or f"Add device-type {payload.manufacturer} {payload.model}"
    try:
        result = github_repo.save_file(
            pat, target.repo, target.branch, path, validated.to_yaml_dict(), commit_message,
            sha=None, pr_body=_with_actor_trailer(payload.pr_body, actor),
        )
    except ValueError as exc:
        _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                    status="error", detail=str(exc), actor=actor)
        raise HTTPException(409, str(exc))
    except Exception as exc:
        _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                    status="error", detail=str(exc), actor=actor)
        raise _github_error_to_http(exc)
    _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                status="success", detail=f"Created, PR #{result['pr_number']}", actor=actor)
    return schemas.SaveResult(**result)


@router.put("/file", response_model=schemas.SaveResult)
def save_device_type(
    target_id: str, payload: schemas.SaveDeviceTypeRequest, request: Request,
    path: str = Query(...), db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    target = _get_target(target_id, db, ctx)
    path = _validated_path(target, path)
    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)

    try:
        validated = DeviceType(**payload.payload)
    except Exception as exc:
        raise HTTPException(422, f"Payload does not match device-type schema: {exc}")

    commit_message = payload.commit_message or f"Update device-type {validated.manufacturer} {validated.model}"
    try:
        result = github_repo.save_file(
            pat, target.repo, target.branch, path, validated.to_yaml_dict(), commit_message,
            sha=payload.sha, pr_body=_with_actor_trailer(payload.pr_body, actor),
        )
    except ValueError as exc:
        _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                    status="error", detail=str(exc), actor=actor)
        raise HTTPException(409, str(exc))
    except Exception as exc:
        _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                    status="error", detail=str(exc), actor=actor)
        raise _github_error_to_http(exc)
    _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                status="success", detail=f"Updated, PR #{result['pr_number']}", actor=actor)
    return schemas.SaveResult(**result)


@router.delete("/file", status_code=204)
def delete_device_type(
    target_id: str, payload: schemas.DeleteDeviceTypeRequest, request: Request,
    path: str = Query(...), db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    target = _get_target(target_id, db, ctx)
    path = _validated_path(target, path)
    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)
    commit_message = payload.commit_message or f"Remove device-type {path}"
    try:
        github_repo.delete_file(pat, target.repo, target.branch, path, payload.sha, commit_message)
    except Exception as exc:
        _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                    status="error", detail=str(exc), actor=actor)
        raise _github_error_to_http(exc)
    _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                status="success", detail="Deleted", actor=actor)


@router.post("/import", response_model=schemas.SaveResult, status_code=201)
def import_yaml(
    target_id: str, payload: schemas.ImportYamlRequest, request: Request, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    target = _get_target(target_id, db, ctx)
    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)

    try:
        raw = yaml.safe_load(payload.yaml_text)
        validated = DeviceType(**raw)
    except Exception as exc:
        raise HTTPException(422, f"YAML does not match device-type schema: {exc}")

    path = _format_path(target, manufacturer=validated.manufacturer, slug=validated.slug, model=validated.model)
    commit_message = payload.commit_message or f"Import device-type {validated.manufacturer} {validated.model}"

    try:
        exists = github_repo.file_exists(pat, target.repo, target.branch, path)
        sha = github_repo.get_file(pat, target.repo, target.branch, path)["sha"] if exists else None
        result = github_repo.save_file(
            pat, target.repo, target.branch, path, validated.to_yaml_dict(), commit_message,
            sha=sha, pr_body=_with_actor_trailer(payload.pr_body, actor),
        )
    except ValueError as exc:
        _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                    status="error", detail=str(exc), actor=actor)
        raise HTTPException(409, str(exc))
    except Exception as exc:
        _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                    status="error", detail=str(exc), actor=actor)
        raise _github_error_to_http(exc)
    _log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name,
                status="success", detail=f"Imported, PR #{result['pr_number']}", actor=actor)
    return schemas.SaveResult(**result)


@router.get("/file/coverage", response_model=list[schemas.CoverageEntry])
def coverage(target_id: str, path: str = Query(...), db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    """
    Answers "which instances have this device type, and are they up to date?"
    across every configured NetBox instance — not just ones it's been pushed to
    before (that's what Drift tracks), and not requiring the caller to pick a
    subset first (that's what diff-with-netbox is for during a push).
    """
    target = _get_target(target_id, db, ctx)
    path = _validated_path(target, path)
    pat = crypto.decrypt(target.pat_encrypted)
    try:
        source = DeviceType(**github_repo.get_file(pat, target.repo, target.branch, path)["payload"]).to_internal_dict()
        source_images, image_warnings = _load_elevation_images(pat, target, path, source)
    except Exception as exc:
        raise _github_error_to_http(exc)

    instances = db.query(models.NetboxInstance).all()
    instances = filter_scoped(instances, "instance", ctx, db)

    def check_one(instance: models.NetboxInstance) -> schemas.CoverageEntry:
        token = crypto.decrypt(instance.api_token_encrypted)
        try:
            existing = netbox_client.get_existing_device_type(
                instance.base_url, token, netbox_client.verify_for_instance(instance), source["manufacturer"], source["slug"]
            )
            changes = _image_changes(
                source, source_images, image_warnings, existing,
                instance.base_url, token, netbox_client.verify_for_instance(instance),
            )
            result = diff_mod.diff_payloads(source, existing, changes)
            return schemas.CoverageEntry(
                instance_id=instance.id, instance_name=instance.name,
                status=result["status"], image_warnings=image_warnings,
            )
        except Exception as exc:
            return schemas.CoverageEntry(instance_id=instance.id, instance_name=instance.name, status="error", error=str(exc))

    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(check_one, inst) for inst in instances]
        for future in as_completed(futures):
            results.append(future.result())

    order = {inst.id: idx for idx, inst in enumerate(instances)}
    results.sort(key=lambda r: order.get(r.instance_id, 0))
    return results


@router.post("/bulk-import/scan", response_model=list[schemas.BulkImportScanEntry])
def bulk_import_scan(
    target_id: str, payload: schemas.BulkImportScanRequest, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    """
    Downloads the repository archive once and reads searchable metadata from
    every device-type YAML without making one GitHub API call per file.
    """
    target = _get_target(target_id, db, ctx)
    source_pat = payload.source_pat or crypto.decrypt(target.pat_encrypted)
    try:
        github_repo.validate_source_coordinates(payload.source_repo, payload.source_branch)
        files = github_repo.scan_repository_paths(
            source_pat, payload.source_repo, payload.source_branch, payload.source_base_dir
        )
    except Exception as exc:
        raise _github_error_to_http(exc)

    return [schemas.BulkImportScanEntry(
        path=item["path"], manufacturer_guess=item.get("manufacturer"), slug_guess=item.get("slug"),
        model=item.get("model"), part_number=item.get("part_number"),
    ) for item in files]


@router.post("/bulk-import/preview", response_model=schemas.DeviceTypePreview)
def bulk_import_preview(
    target_id: str, payload: schemas.BulkImportPreviewRequest, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    """
    Fetches and parses one candidate file on demand, so the picker can show
    its component counts and custom field values before it's imported —
    deliberately separate from bulk_import_scan, which stays cheap by only
    listing paths.
    """
    target = _get_target(target_id, db, ctx)
    source_pat = payload.source_pat or crypto.decrypt(target.pat_encrypted)
    try:
        github_repo.validate_source_coordinates(payload.source_repo, payload.source_branch)
        content = github_repo.get_file(source_pat, payload.source_repo, payload.source_branch, payload.path)
    except Exception as exc:
        raise _github_error_to_http(exc)
    try:
        validated = DeviceType(**content["payload"])
    except Exception as exc:
        raise HTTPException(422, f"Could not parse this file as a device type: {exc}")
    preview = _to_preview(validated)
    for side in ("front", "rear"):
        image = github_repo.get_elevation_image(
            source_pat, payload.source_repo, payload.source_branch, payload.path, validated.slug, side
        )
        if image or getattr(validated, f"{side}_image"):
            preview.image_status[side] = "present" if image else "missing"
    return preview


@router.post("/bulk-import/image-preview", response_model=schemas.ImagePreviewOut)
def bulk_import_image_preview(target_id: str, payload: schemas.BulkImportImagePreviewRequest,
                              db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx); source_pat = payload.source_pat or crypto.decrypt(target.pat_encrypted)
    source = DeviceType(**github_repo.get_file(
        source_pat, payload.source_repo, payload.source_branch, payload.path
    )["payload"])
    image = github_repo.get_elevation_image(
        source_pat, payload.source_repo, payload.source_branch, payload.path, source.slug, payload.side
    )
    if image is None:
        raise HTTPException(404, f"Declared {payload.side} image is missing from the source repository.")
    return schemas.ImagePreviewOut(side=payload.side, filename=image.path.rsplit("/", 1)[-1],
        content_type=image.content_type, content_base64=base64.b64encode(image.content).decode("ascii"))


@router.post("/bulk-import", response_model=schemas.BulkImportResult)
def bulk_import(
    target_id: str, payload: schemas.BulkImportRequest, request: Request, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    """
    Fetches each selected file from the source repo, validates it against the
    device-type schema, and lands all of them on ONE shared branch with ONE
    pull request against the target repo — not one PR per file.
    """
    target = _get_target(target_id, db, ctx)
    pat = crypto.decrypt(target.pat_encrypted)
    source_pat = payload.source_pat or pat
    actor = get_current_actor(request)

    if not payload.paths:
        raise HTTPException(400, "No files selected to import.")
    try:
        github_repo.validate_source_coordinates(payload.source_repo, payload.source_branch)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    files_to_commit = []
    prevalidation_failures = []
    for source_path in payload.paths:
        try:
            source_file = github_repo.get_file(source_pat, payload.source_repo, payload.source_branch, source_path)
            validated = DeviceType(**source_file["payload"])
        except Exception as exc:
            prevalidation_failures.append(schemas.BulkImportFailure(path=source_path, error=str(exc)))
            continue
        dest_path = _format_path(target, manufacturer=validated.manufacturer,
                                 slug=validated.slug, model=validated.model)
        normalized = validated.to_internal_dict()
        image_items = []
        for side in ("front", "rear"):
            try:
                image = github_repo.get_elevation_image(
                    source_pat, payload.source_repo, payload.source_branch,
                    source_path, validated.slug, side,
                )
                if image is None and getattr(validated, f"{side}_image"):
                    raise ValueError(f"Declared {side} image is missing from the source repository")
                if image is None:
                    continue
                normalized[f"{side}_image"] = True
                image_path = github_repo.elevation_image_destination(
                    dest_path, validated.slug, side, image.path
                )
                image_items.append({"path": image_path, "content": image.content, "source_path": image.path})
            except Exception as exc:
                prevalidation_failures.append(schemas.BulkImportFailure(
                    path=f"{source_path} ({side} image)", error=str(exc)
                ))
        files_to_commit.append({"path": dest_path, "payload": DeviceType(**normalized).to_yaml_dict(), "source_path": source_path})
        files_to_commit.extend(image_items)

    branch_name = f"bulk-import/{uuid.uuid4().hex[:10]}"
    commit_message_prefix = payload.commit_message or f"Bulk import from {payload.source_repo}"

    try:
        commit_result = github_repo.bulk_create_files(
            pat, target.repo, target.branch, branch_name,
            files_to_commit,
            commit_message_prefix,
        )
    except Exception as exc:
        _log_action(db, repo_target_id=target_id, file_path=f"bulk-import ({len(payload.paths)} files)",
                    target_name=target.name, status="error", detail=str(exc), actor=actor)
        raise _github_error_to_http(exc)

    all_failed = prevalidation_failures + [schemas.BulkImportFailure(**f) for f in commit_result["failed"]]
    pr_number, pr_url = None, None

    if commit_result["created"]:
        pr_title = payload.pr_title or f"Bulk import {len(commit_result['created'])} device types from {payload.source_repo}"
        pr_body_lines = [
            f"Imports {len(commit_result['created'])} device type(s) from `{payload.source_repo}`@`{payload.source_branch}`:",
            "",
            *[f"- {p}" for p in commit_result["created"]],
        ]
        if commit_result["skipped"]:
            pr_body_lines += ["", f"Skipped ({len(commit_result['skipped'])}, already exist at destination):",
                               *[f"- {p}" for p in commit_result["skipped"]]]
        if all_failed:
            pr_body_lines += ["", f"Failed ({len(all_failed)}):", *[f"- {f.path}: {f.error}" for f in all_failed]]
        pr_body = payload.pr_body or "\n".join(pr_body_lines)

        try:
            pr_result = github_repo.open_bulk_pr(
                pat, target.repo, branch_name, target.branch, pr_title, _with_actor_trailer(pr_body, actor)
            )
            pr_number, pr_url = pr_result["pr_number"], pr_result["pr_url"]
        except Exception as exc:
            all_failed.append(schemas.BulkImportFailure(path="(PR creation)", error=str(exc)))

    status = "success" if commit_result["created"] and not all_failed else ("error" if not commit_result["created"] else "success")
    detail = f"Imported {len(commit_result['created'])}, skipped {len(commit_result['skipped'])}, failed {len(all_failed)}."
    if pr_number:
        detail += f" PR #{pr_number}."
    _log_action(db, repo_target_id=target_id, file_path=f"bulk-import ({len(payload.paths)} files)",
                target_name=target.name, status=status, detail=detail, actor=actor)

    return schemas.BulkImportResult(
        branch=branch_name, pr_number=pr_number, pr_url=pr_url,
        imported=commit_result["created"], skipped_existing=commit_result["skipped"], failed=all_failed,
    )


@router.post("/ndx/search", response_model=list[schemas.NdxSearchEntry])
def ndx_search(target_id: str, payload: schemas.NdxSearchRequest, db: Session = Depends(get_db),
               ctx: AccessContext = Depends(require_role("editor"))):
    _get_target(target_id, db, ctx)
    try:
        return ndx_client.search(payload.query, max(1, min(payload.limit, 500)))
    except Exception as exc:
        raise HTTPException(502, f"Could not search the NDX catalog: {exc}")


@router.post("/ndx/preview", response_model=schemas.DeviceTypePreview)
def ndx_preview(target_id: str, payload: schemas.NdxDeviceKey, db: Session = Depends(get_db),
                ctx: AccessContext = Depends(require_role("editor"))):
    _get_target(target_id, db, ctx)
    try:
        return _to_preview(DeviceType(**ndx_client.get_yaml(payload.vendor_slug, payload.slug)))
    except Exception as exc:
        raise HTTPException(502, f"Could not load this NDX device type: {exc}")


@router.post("/ndx/import", response_model=schemas.BulkImportResult)
def ndx_import(target_id: str, payload: schemas.NdxImportRequest, request: Request,
               db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    target = _get_target(target_id, db, ctx)
    if not payload.selections:
        raise HTTPException(400, "No NDX device types selected to import.")
    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)
    files_to_commit, failures = [], []

    def fetch_selection(selection):
        source_key = f"{selection.vendor_slug}/{selection.slug}"
        try:
            validated = DeviceType(**ndx_client.get_yaml(selection.vendor_slug, selection.slug))
            destination = _format_path(target, manufacturer=validated.manufacturer,
                                       slug=validated.slug, model=validated.model)
            return {"path": destination, "payload": validated.to_yaml_dict(), "source": source_key}, None
        except Exception as exc:
            return None, schemas.BulkImportFailure(path=source_key, error=str(exc))

    with ThreadPoolExecutor(max_workers=min(8, len(payload.selections))) as executor:
        futures = [executor.submit(fetch_selection, selection) for selection in payload.selections]
        for future in as_completed(futures):
            item, failure = future.result()
            if item:
                files_to_commit.append(item)
            if failure:
                failures.append(failure)
    audit_file_path = f"ndx-import ({len(payload.selections)} device types)"
    if not files_to_commit and failures:
        detail = f"Could not download any selected NDX definitions: {failures[0].error}"
        _log_action(db, repo_target_id=target_id, file_path=audit_file_path,
                    target_name=target.name, status="error", detail=detail, actor=actor)
        raise HTTPException(502, detail)

    branch_name = f"import-from-ndx/{uuid.uuid4().hex[:10]}"
    try:
        commit_result = github_repo.bulk_create_files(
            pat, target.repo, target.branch, branch_name, files_to_commit,
            payload.commit_message or "Import device types from NetBox Data Exchange",
        )
    except Exception as exc:
        _log_action(db, repo_target_id=target_id, file_path=audit_file_path,
                    target_name=target.name, status="error", detail=str(exc), actor=actor)
        raise _github_error_to_http(exc)
    failures += [schemas.BulkImportFailure(**item) for item in commit_result["failed"]]
    pr_number = pr_url = None
    if commit_result["created"]:
        title = payload.pr_title or f"Import {len(commit_result['created'])} device types from NDX"
        body = payload.pr_body or "\n".join([
            f"Imports {len(commit_result['created'])} device type(s) from NetBox Data Exchange:", "",
            *[f"- NDX `{item['source']}` -> `{item['path']}`" for item in files_to_commit
              if item["path"] in commit_result["created"]],
        ])
        try:
            pr = github_repo.open_bulk_pr(
                pat, target.repo, branch_name, target.branch, title, _with_actor_trailer(body, actor)
            )
            pr_number, pr_url = pr["pr_number"], pr["pr_url"]
        except Exception as exc:
            failures.append(schemas.BulkImportFailure(path="(PR creation)", error=str(exc)))
    detail = f"Imported {len(commit_result['created'])}, skipped {len(commit_result['skipped'])}, failed {len(failures)}."
    _log_action(db, repo_target_id=target_id, file_path=audit_file_path,
                target_name=target.name, status="success" if commit_result["created"] else "error",
                detail=detail, actor=actor)
    return schemas.BulkImportResult(
        branch=branch_name, pr_number=pr_number, pr_url=pr_url, imported=commit_result["created"],
        skipped_existing=commit_result["skipped"], failed=failures,
    )


@router.post("/import-from-netbox/scan", response_model=list[schemas.ImportFromNetboxScanEntry])
def import_from_netbox_scan(
    target_id: str, payload: schemas.ImportFromNetboxScanRequest, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    _get_target(target_id, db, ctx)  # 404s early if the repo target itself is bogus or scoped out
    instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")
    token = crypto.decrypt(instance.api_token_encrypted)
    try:
        results = netbox_client.list_device_types_on_instance(instance.base_url, token, netbox_client.verify_for_instance(instance))
    except Exception as exc:
        raise HTTPException(502, f"Could not list device types from {instance.name}: {exc}")
    return [schemas.ImportFromNetboxScanEntry(**r) for r in results]


@router.post("/import-from-netbox/preview", response_model=schemas.DeviceTypePreview)
def import_from_netbox_preview(
    target_id: str, payload: schemas.ImportFromNetboxPreviewRequest, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    """
    Fetches one candidate's full definition from the instance on demand
    (component templates and custom field values included) so the picker can
    show what's actually there before it's imported — the scan list above
    stays cheap on purpose (no per-device-type detail).
    """
    _get_target(target_id, db, ctx)
    instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")
    token = crypto.decrypt(instance.api_token_encrypted)
    try:
        fetched = netbox_client.get_existing_device_type(
            instance.base_url, token, netbox_client.verify_for_instance(instance), payload.manufacturer, payload.slug
        )
    except Exception as exc:
        raise HTTPException(502, f"Could not fetch this device type from {instance.name}: {exc}")
    if fetched is None:
        raise HTTPException(404, "No longer found on this instance.")
    try:
        validated = DeviceType(**fetched)
    except Exception as exc:
        raise HTTPException(422, f"NetBox returned a device type that doesn't match our schema: {exc}")
    preview = _to_preview(validated)
    preview.image_status = {
        side: ("present" if url else "missing")
        for side, url in (fetched.get("_image_urls") or {}).items()
        if url or getattr(validated, f"{side}_image", False)
    }
    return preview


@router.post("/import-from-netbox/image-preview", response_model=schemas.ImagePreviewOut)
def import_from_netbox_image_preview(target_id: str, payload: schemas.ImportFromNetboxImagePreviewRequest,
                                     db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor"))):
    _get_target(target_id, db, ctx); instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")
    token = crypto.decrypt(instance.api_token_encrypted)
    fetched = netbox_client.get_existing_device_type(
        instance.base_url, token, netbox_client.verify_for_instance(instance), payload.manufacturer, payload.slug
    )
    image_url = (fetched or {}).get("_image_urls", {}).get(payload.side)
    if not image_url:
        raise HTTPException(404, f"No {payload.side} image found on this instance.")
    image = netbox_client.get_image_file(str(image_url), token, netbox_client.verify_for_instance(instance), instance.base_url)
    return schemas.ImagePreviewOut(side=payload.side, filename=image.path,
        content_type=image.content_type, content_base64=base64.b64encode(image.content).decode("ascii"))


@router.post("/import-from-netbox", response_model=schemas.BulkImportResult)
def import_from_netbox(
    target_id: str, payload: schemas.ImportFromNetboxRequest, request: Request, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    """
    Fetches each selected device type's full definition (components included)
    from a NetBox instance and lands all of them on ONE shared branch with ONE
    pull request against the target repo — the same batching approach as the
    library bulk-import, just with a NetBox instance as the source instead of
    another git repo. Propagating the result on to *other* NetBox instances is
    deliberately a separate, later step (the normal Publish tab, once this PR
    is reviewed and merged) rather than something this endpoint also does —
    pushing straight from an unreviewed import would bypass the PR-only and
    approval-gate guarantees the rest of the app relies on.
    """
    target = _get_target(target_id, db, ctx)
    instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")

    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)

    if not payload.selections:
        raise HTTPException(400, "No device types selected to import.")

    token = crypto.decrypt(instance.api_token_encrypted)
    files_to_commit = []
    prevalidation_failures = []
    for sel in payload.selections:
        key = f"{sel.manufacturer}/{sel.slug}"
        try:
            fetched = netbox_client.get_existing_device_type(instance.base_url, token, netbox_client.verify_for_instance(instance), sel.manufacturer, sel.slug)
            if fetched is None:
                raise ValueError("No longer found on this instance.")
            validated = DeviceType(**fetched)
        except Exception as exc:
            prevalidation_failures.append(schemas.BulkImportFailure(path=key, error=str(exc)))
            continue
        dest_path = _format_path(target, manufacturer=validated.manufacturer,
                                 slug=validated.slug, model=validated.model)
        files_to_commit.append({"path": dest_path, "payload": validated.to_yaml_dict()})
        for side, image_url in (fetched.get("_image_urls") or {}).items():
            if not image_url:
                continue
            try:
                absolute_url = urljoin(instance.base_url.rstrip("/") + "/", str(image_url))
                image = netbox_client.get_image_file(absolute_url, token, netbox_client.verify_for_instance(instance), instance.base_url)
                image_path = github_repo.elevation_image_destination(
                    dest_path, validated.slug, side, image.path
                )
                files_to_commit.append({"path": image_path, "content": image.content})
            except Exception as exc:
                prevalidation_failures.append(schemas.BulkImportFailure(
                    path=f"{key} ({side} image)", error=str(exc)
                ))

    branch_name = f"import-from-netbox/{uuid.uuid4().hex[:10]}"
    commit_message_prefix = payload.commit_message or f"Import from NetBox instance {instance.name}"
    audit_file_path = f"import-from-netbox ({len(payload.selections)} device types from {instance.name})"

    try:
        commit_result = github_repo.bulk_create_files(pat, target.repo, target.branch, branch_name, files_to_commit, commit_message_prefix)
    except Exception as exc:
        _log_action(db, repo_target_id=target_id, file_path=audit_file_path,
                    target_name=target.name, status="error", detail=str(exc), actor=actor)
        raise _github_error_to_http(exc)

    all_failed = prevalidation_failures + [schemas.BulkImportFailure(**f) for f in commit_result["failed"]]
    pr_number, pr_url = None, None

    if commit_result["created"]:
        pr_title = payload.pr_title or f"Import {len(commit_result['created'])} device types from {instance.name}"
        pr_body_lines = [
            f"Imports {len(commit_result['created'])} device type(s) from NetBox instance **{instance.name}**:",
            "", *[f"- {p}" for p in commit_result["created"]],
        ]
        if commit_result["skipped"]:
            pr_body_lines += ["", f"Skipped ({len(commit_result['skipped'])}, already exist at destination):",
                               *[f"- {p}" for p in commit_result["skipped"]]]
        if all_failed:
            pr_body_lines += ["", f"Failed ({len(all_failed)}):", *[f"- {f.path}: {f.error}" for f in all_failed]]
        pr_body = payload.pr_body or "\n".join(pr_body_lines)

        try:
            pr_result = github_repo.open_bulk_pr(pat, target.repo, branch_name, target.branch, pr_title, _with_actor_trailer(pr_body, actor))
            pr_number, pr_url = pr_result["pr_number"], pr_result["pr_url"]
        except Exception as exc:
            all_failed.append(schemas.BulkImportFailure(path="(PR creation)", error=str(exc)))

    status = "success" if commit_result["created"] else "error"
    detail = f"Imported {len(commit_result['created'])}, skipped {len(commit_result['skipped'])}, failed {len(all_failed)}."
    if pr_number:
        detail += f" PR #{pr_number}."
    _log_action(db, repo_target_id=target_id, file_path=audit_file_path,
                target_name=target.name, status=status, detail=detail, actor=actor)

    return schemas.BulkImportResult(
        branch=branch_name, pr_number=pr_number, pr_url=pr_url,
        imported=commit_result["created"], skipped_existing=commit_result["skipped"], failed=all_failed,
    )


@router.post("/file/diff-with-netbox", response_model=list[schemas.InstanceDiffResult])
def diff_with_netbox(
    target_id: str, payload: schemas.PushToNetboxRequest, path: str = Query(...), db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    target = _get_target(target_id, db, ctx)
    path = _validated_path(target, path)
    pat = crypto.decrypt(target.pat_encrypted)
    try:
        source = DeviceType(**github_repo.get_file(pat, target.repo, target.branch, path)["payload"]).to_internal_dict()
        source_images, image_warnings = _load_elevation_images(pat, target, path, source)
    except Exception as exc:
        raise _github_error_to_http(exc)

    instances = _resolve_instances(db, payload.instance_ids, payload.tags, ctx)
    results = []
    for instance in instances:
        if not has_role_at_least(role_for_resource(ctx, "instance", instance.id), "editor"):
            results.append(schemas.InstanceDiffResult(
                instance_id=instance.id, instance_name=instance.name,
                error="Blocked: editor role is required on this instance.",
            ))
            continue
        token = crypto.decrypt(instance.api_token_encrypted)
        try:
            existing = netbox_client.get_existing_device_type(
                instance.base_url, token, netbox_client.verify_for_instance(instance), source["manufacturer"], source["slug"]
            )
            changes = _image_changes(
                source, source_images, image_warnings, existing,
                instance.base_url, token, netbox_client.verify_for_instance(instance),
            )
            diff = diff_mod.diff_payloads(source, existing, changes)
            results.append(schemas.InstanceDiffResult(instance_id=instance.id, instance_name=instance.name, diff=diff))
        except Exception as exc:
            results.append(schemas.InstanceDiffResult(instance_id=instance.id, instance_name=instance.name, error=str(exc)))
    return results


@router.post("/file/push-to-netbox", response_model=list[schemas.PushResultItem])
def push_to_netbox(
    target_id: str, payload: schemas.PushToNetboxRequest, request: Request,
    path: str = Query(...), db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    target = _get_target(target_id, db, ctx)
    path = _validated_path(target, path)
    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)
    try:
        device_type = github_repo.get_file(pat, target.repo, target.branch, path)["payload"]
        normalized = DeviceType(**device_type).to_internal_dict()
        source_images, image_warnings = _load_elevation_images(pat, target, path, normalized)
    except Exception as exc:
        raise _github_error_to_http(exc)

    instances = _resolve_instances(db, payload.instance_ids, payload.tags, ctx)
    if not instances:
        raise HTTPException(400, "No matching NetBox instances (check instance_ids/tags).")

    results = []
    for instance in instances:
        if not has_role_at_least(role_for_resource(ctx, "instance", instance.id), "editor"):
            detail = "Blocked: editor role is required on this instance."
            results.append(schemas.PushResultItem(target=instance.name, status="error", detail=detail))
            continue
        if instance.requires_approved_pr:
            if not has_role_at_least(role_for_resource(ctx, "instance", instance.id), "admin"):
                detail = "Blocked: this instance requires the admin role to push to (it's flagged 'requires an approved PR')."
                results.append(schemas.PushResultItem(target=instance.name, status="error", detail=detail))
                db.add(models.DeviceTypePushHistory(
                    repo_target_id=target_id, file_path=path, target_type="netbox", target_name=instance.name,
                    status="error", detail=detail,
                    actor_sub=actor.get("sub"), actor_name=actor.get("name"), actor_email=actor.get("email"),
                ))
                continue
            try:
                approval = github_repo.get_merged_pr_approval(pat, target.repo, target.branch, path)
            except Exception as exc:
                results.append(schemas.PushResultItem(
                    target=instance.name, status="error",
                    detail=f"Could not verify PR approval (blocked, this instance requires it): {exc}",
                ))
                db.add(models.DeviceTypePushHistory(
                    repo_target_id=target_id, file_path=path, target_type="netbox", target_name=instance.name,
                    status="error", detail=f"Could not verify PR approval: {exc}",
                    actor_sub=actor.get("sub"), actor_name=actor.get("name"), actor_email=actor.get("email"),
                ))
                continue
            if not approval["found"]:
                detail = ("Blocked: this instance requires an approved, merged PR, but no merged PR "
                          "was found for the current file content.")
                results.append(schemas.PushResultItem(target=instance.name, status="error", detail=detail))
                db.add(models.DeviceTypePushHistory(
                    repo_target_id=target_id, file_path=path, target_type="netbox", target_name=instance.name,
                    status="error", detail=detail,
                    actor_sub=actor.get("sub"), actor_name=actor.get("name"), actor_email=actor.get("email"),
                ))
                continue
            if not approval["approved"]:
                detail = (f"Blocked: PR #{approval['pr_number']} merged this change but has no approving "
                          f"review. This instance requires an approved PR before pushing.")
                results.append(schemas.PushResultItem(target=instance.name, status="error", detail=detail))
                db.add(models.DeviceTypePushHistory(
                    repo_target_id=target_id, file_path=path, target_type="netbox", target_name=instance.name,
                    status="error", detail=detail,
                    actor_sub=actor.get("sub"), actor_name=actor.get("name"), actor_email=actor.get("email"),
                ))
                continue

        token = crypto.decrypt(instance.api_token_encrypted)
        try:
            outcome = netbox_client.push_device_type(
                instance.base_url, token, netbox_client.verify_for_instance(instance), device_type, payload.overwrite,
                images=source_images, image_warnings=image_warnings,
            )
        except Exception as exc:
            outcome = {"status": "error", "detail": str(exc)}
        results.append(schemas.PushResultItem(target=instance.name, **outcome))

        db.add(models.DeviceTypePushHistory(
            repo_target_id=target_id, file_path=path, target_type="netbox", target_name=instance.name,
            status=outcome["status"], detail=outcome.get("detail"),
            actor_sub=actor.get("sub"), actor_name=actor.get("name"), actor_email=actor.get("email"),
        ))
    db.commit()
    return results
