from concurrent.futures import ThreadPoolExecutor, as_completed
import uuid

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from github import GithubException
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.auth import get_current_actor
from app.database import get_db
from app.devicetype_schema import COMPONENT_ENDPOINTS, DeviceType
from app.rbac import AccessContext, filter_scoped, get_access_context, has_role_at_least, require_role, require_visible, role_for_resource
from app.services import diff as diff_mod
from app.services import github_repo, netbox_client
from app.services.github_repo import RepoAccessError

router = APIRouter(prefix="/api/repos/{target_id}/device-types", tags=["device-types"])


def _get_target(target_id: str, db: Session, ctx: AccessContext | None = None) -> models.GithubTarget:
    target = db.get(models.GithubTarget, target_id)
    if not target:
        raise HTTPException(404, "GitHub target not found.")
    if ctx is not None:
        require_visible("github_target", target_id, ctx, db)
    return target


def _base_dir(target: models.GithubTarget) -> str:
    # e.g. "device-types/{manufacturer}/{slug}.yml" -> "device-types"
    return target.path_pattern.split("{")[0].rsplit("/", 1)[0] if "{" in target.path_pattern else target.path_pattern


def _github_error_to_http(exc: Exception) -> HTTPException:
    if isinstance(exc, RepoAccessError):
        return HTTPException(404, str(exc))
    if isinstance(exc, GithubException):
        detail = exc.data.get("message", str(exc)) if isinstance(exc.data, dict) else str(exc)
        return HTTPException(exc.status if isinstance(exc.status, int) else 502, f"GitHub API error: {detail}")
    return HTTPException(500, str(exc))


def _with_actor_trailer(pr_body: str | None, actor: dict) -> str:
    """
    Appended server-side, after whatever the user wrote — even if they cleared
    the auto-generated description, this line still lands in the PR, since the
    whole point is an accountability trail the requester can't accidentally
    drop by editing the free-text field.
    """
    who = actor["name"] or "anonymous"
    if actor.get("email"):
        who = f"{who} <{actor['email']}>"
    trailer = f"\n\n---\nRequested via NetBox Manager by: {who}"
    return (pr_body or "").rstrip() + trailer


def _log_action(db: Session, *, repo_target_id: str, file_path: str, target_name: str,
                 status: str, detail: str | None, actor: dict) -> None:
    db.add(models.DeviceTypePushHistory(
        repo_target_id=repo_target_id, file_path=file_path, target_type="github", target_name=target_name,
        status=status, detail=detail,
        actor_sub=actor.get("sub"), actor_name=actor.get("name"), actor_email=actor.get("email"),
    ))
    db.commit()


def _to_preview(validated: DeviceType) -> schemas.DeviceTypePreview:
    """Shared by both bulk-import preview endpoints (GitHub-library and NetBox-instance sources)."""
    data = validated.to_yaml_dict()
    component_counts = {key: len(data.get(key) or []) for key in COMPONENT_ENDPOINTS}
    component_counts = {k: v for k, v in component_counts.items() if v}
    return schemas.DeviceTypePreview(
        manufacturer=validated.manufacturer, model=validated.model, slug=validated.slug,
        component_counts=component_counts, custom_fields=validated.custom_fields,
    )


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
                manufacturer=payload.get("manufacturer"),
                model=payload.get("model"),
                slug=payload.get("slug"),
            ))
        except Exception:
            # Skip files that aren't parseable YAML device-types rather than failing the whole list.
            summaries.append(schemas.DeviceTypeSummary(path=f.path))
    return summaries


@router.get("/file", response_model=schemas.DeviceTypeFileOut)
def get_device_type(target_id: str, path: str = Query(...), db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    target = _get_target(target_id, db, ctx)
    pat = crypto.decrypt(target.pat_encrypted)
    try:
        working_branch = github_repo.resolve_working_branch(pat, target.repo, target.branch, path)
        result = github_repo.get_file(pat, target.repo, working_branch, path)
        open_pr = github_repo.get_open_pr(pat, target.repo, target.branch, path)
    except Exception as exc:
        raise _github_error_to_http(exc)
    return schemas.DeviceTypeFileOut(
        repo_target_id=target_id, path=path, sha=result["sha"], payload=result["payload"], open_pr=open_pr
    )


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
    path = target.path_pattern.format(manufacturer=payload.manufacturer, slug=payload.slug, model=payload.model)

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

    path = target.path_pattern.format(manufacturer=validated.manufacturer, slug=validated.slug, model=validated.model)
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
    pat = crypto.decrypt(target.pat_encrypted)
    try:
        source = github_repo.get_file(pat, target.repo, target.branch, path)["payload"]
    except Exception as exc:
        raise _github_error_to_http(exc)

    instances = db.query(models.NetboxInstance).all()
    instances = filter_scoped(instances, "instance", ctx, db)

    def check_one(instance: models.NetboxInstance) -> schemas.CoverageEntry:
        token = crypto.decrypt(instance.api_token_encrypted)
        try:
            existing = netbox_client.get_existing_device_type(
                instance.base_url, token, instance.verify_ssl, source["manufacturer"], source["slug"]
            )
            result = diff_mod.diff_payloads(source, existing)
            return schemas.CoverageEntry(instance_id=instance.id, instance_name=instance.name, status=result["status"])
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
    Lists every .yml/.yaml file under the source repo's device-type directory.
    Cheap by design (one git-trees API call) — manufacturer/slug are guessed
    from the path, not fetched from file content, so this stays fast even
    against something the size of the full community library.
    """
    target = _get_target(target_id, db, ctx)
    source_pat = payload.source_pat or crypto.decrypt(target.pat_encrypted)
    try:
        files = github_repo.list_device_types(source_pat, payload.source_repo, payload.source_branch, payload.source_base_dir)
    except Exception as exc:
        raise _github_error_to_http(exc)

    entries = []
    for f in files:
        manufacturer, slug = github_repo.guess_manufacturer_slug(f.path)
        entries.append(schemas.BulkImportScanEntry(path=f.path, manufacturer_guess=manufacturer, slug_guess=slug))
    return entries


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
        content = github_repo.get_file(source_pat, payload.source_repo, payload.source_branch, payload.path)
    except Exception as exc:
        raise _github_error_to_http(exc)
    try:
        validated = DeviceType(**content["payload"])
    except Exception as exc:
        raise HTTPException(422, f"Could not parse this file as a device type: {exc}")
    return _to_preview(validated)


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

    files_to_commit = []
    prevalidation_failures = []
    for source_path in payload.paths:
        try:
            source_file = github_repo.get_file(source_pat, payload.source_repo, payload.source_branch, source_path)
            validated = DeviceType(**source_file["payload"])
        except Exception as exc:
            prevalidation_failures.append(schemas.BulkImportFailure(path=source_path, error=str(exc)))
            continue
        dest_path = target.path_pattern.format(
            manufacturer=validated.manufacturer, slug=validated.slug, model=validated.model
        )
        files_to_commit.append({"path": dest_path, "payload": validated.to_yaml_dict(), "source_path": source_path})

    branch_name = f"bulk-import/{uuid.uuid4().hex[:10]}"
    commit_message_prefix = payload.commit_message or f"Bulk import from {payload.source_repo}"

    try:
        commit_result = github_repo.bulk_create_files(
            pat, target.repo, target.branch, branch_name,
            [{"path": f["path"], "payload": f["payload"]} for f in files_to_commit],
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
        results = netbox_client.list_device_types_on_instance(instance.base_url, token, instance.verify_ssl)
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
            instance.base_url, token, instance.verify_ssl, payload.manufacturer, payload.slug
        )
    except Exception as exc:
        raise HTTPException(502, f"Could not fetch this device type from {instance.name}: {exc}")
    if fetched is None:
        raise HTTPException(404, "No longer found on this instance.")
    try:
        validated = DeviceType(**fetched)
    except Exception as exc:
        raise HTTPException(422, f"NetBox returned a device type that doesn't match our schema: {exc}")
    return _to_preview(validated)


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
            fetched = netbox_client.get_existing_device_type(instance.base_url, token, instance.verify_ssl, sel.manufacturer, sel.slug)
            if fetched is None:
                raise ValueError("No longer found on this instance.")
            validated = DeviceType(**fetched)
        except Exception as exc:
            prevalidation_failures.append(schemas.BulkImportFailure(path=key, error=str(exc)))
            continue
        dest_path = target.path_pattern.format(manufacturer=validated.manufacturer, slug=validated.slug, model=validated.model)
        files_to_commit.append({"path": dest_path, "payload": validated.to_yaml_dict()})

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
    pat = crypto.decrypt(target.pat_encrypted)
    try:
        source = github_repo.get_file(pat, target.repo, target.branch, path)["payload"]
    except Exception as exc:
        raise _github_error_to_http(exc)

    instances = _resolve_instances(db, payload.instance_ids, payload.tags, ctx)
    results = []
    for instance in instances:
        token = crypto.decrypt(instance.api_token_encrypted)
        try:
            existing = netbox_client.get_existing_device_type(
                instance.base_url, token, instance.verify_ssl, source["manufacturer"], source["slug"]
            )
            diff = diff_mod.diff_payloads(source, existing)
            results.append(schemas.InstanceDiffResult(instance_id=instance.id, instance_name=instance.name, diff=diff))
        except Exception as exc:
            results.append(schemas.InstanceDiffResult(instance_id=instance.id, instance_name=instance.name, error=str(exc)))
    return results


def _resolve_instances(db: Session, instance_ids: list[str], tags: list[str], ctx: AccessContext | None = None) -> list[models.NetboxInstance]:
    wanted_ids = set(instance_ids)
    wanted_tags = set(tags)
    if not wanted_ids and not wanted_tags:
        return []
    all_instances = db.query(models.NetboxInstance).all()
    if ctx is not None:
        all_instances = filter_scoped(all_instances, "instance", ctx, db)
    seen = {}
    for inst in all_instances:
        if inst.id in wanted_ids or (wanted_tags & set(inst.tags)):
            seen[inst.id] = inst
    return list(seen.values())


@router.post("/file/push-to-netbox", response_model=list[schemas.PushResultItem])
def push_to_netbox(
    target_id: str, payload: schemas.PushToNetboxRequest, request: Request,
    path: str = Query(...), db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    target = _get_target(target_id, db, ctx)
    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)
    try:
        device_type = github_repo.get_file(pat, target.repo, target.branch, path)["payload"]
    except Exception as exc:
        raise _github_error_to_http(exc)

    instances = _resolve_instances(db, payload.instance_ids, payload.tags, ctx)
    if not instances:
        raise HTTPException(400, "No matching NetBox instances (check instance_ids/tags).")

    results = []
    for instance in instances:
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
                instance.base_url, token, instance.verify_ssl, device_type, payload.overwrite
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
