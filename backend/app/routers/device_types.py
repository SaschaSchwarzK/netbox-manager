from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from github import GithubException
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.auth import get_current_actor
from app.database import get_db
from app.devicetype_schema import DeviceType
from app.services import diff as diff_mod
from app.services import github_repo, netbox_client
from app.services.github_repo import RepoAccessError

router = APIRouter(prefix="/api/repos/{target_id}/device-types", tags=["device-types"])


def _get_target(target_id: str, db: Session) -> models.GithubTarget:
    target = db.get(models.GithubTarget, target_id)
    if not target:
        raise HTTPException(404, "GitHub target not found.")
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


@router.get("", response_model=list[schemas.DeviceTypeSummary])
def list_device_types(target_id: str, db: Session = Depends(get_db)):
    target = _get_target(target_id, db)
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
def get_device_type(target_id: str, path: str = Query(...), db: Session = Depends(get_db)):
    target = _get_target(target_id, db)
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
def create_device_type(target_id: str, payload: schemas.CreateDeviceTypeRequest, request: Request, db: Session = Depends(get_db)):
    target = _get_target(target_id, db)
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
    path: str = Query(...), db: Session = Depends(get_db)
):
    target = _get_target(target_id, db)
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
    path: str = Query(...), db: Session = Depends(get_db)
):
    target = _get_target(target_id, db)
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
def import_yaml(target_id: str, payload: schemas.ImportYamlRequest, request: Request, db: Session = Depends(get_db)):
    target = _get_target(target_id, db)
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
def coverage(target_id: str, path: str = Query(...), db: Session = Depends(get_db)):
    """
    Answers "which instances have this device type, and are they up to date?"
    across every configured NetBox instance — not just ones it's been pushed to
    before (that's what Drift tracks), and not requiring the caller to pick a
    subset first (that's what diff-with-netbox is for during a push).
    """
    target = _get_target(target_id, db)
    pat = crypto.decrypt(target.pat_encrypted)
    try:
        source = github_repo.get_file(pat, target.repo, target.branch, path)["payload"]
    except Exception as exc:
        raise _github_error_to_http(exc)

    instances = db.query(models.NetboxInstance).all()

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


@router.post("/file/diff-with-netbox", response_model=list[schemas.InstanceDiffResult])
def diff_with_netbox(
    target_id: str, payload: schemas.PushToNetboxRequest, path: str = Query(...), db: Session = Depends(get_db)
):
    target = _get_target(target_id, db)
    pat = crypto.decrypt(target.pat_encrypted)
    try:
        source = github_repo.get_file(pat, target.repo, target.branch, path)["payload"]
    except Exception as exc:
        raise _github_error_to_http(exc)

    instances = _resolve_instances(db, payload.instance_ids, payload.tags)
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


def _resolve_instances(db: Session, instance_ids: list[str], tags: list[str]) -> list[models.NetboxInstance]:
    wanted_ids = set(instance_ids)
    wanted_tags = set(tags)
    if not wanted_ids and not wanted_tags:
        return []
    all_instances = db.query(models.NetboxInstance).all()
    seen = {}
    for inst in all_instances:
        if inst.id in wanted_ids or (wanted_tags & set(inst.tags)):
            seen[inst.id] = inst
    return list(seen.values())


@router.post("/file/push-to-netbox", response_model=list[schemas.PushResultItem])
def push_to_netbox(
    target_id: str, payload: schemas.PushToNetboxRequest, request: Request,
    path: str = Query(...), db: Session = Depends(get_db)
):
    target = _get_target(target_id, db)
    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)
    try:
        device_type = github_repo.get_file(pat, target.repo, target.branch, path)["payload"]
    except Exception as exc:
        raise _github_error_to_http(exc)

    instances = _resolve_instances(db, payload.instance_ids, payload.tags)
    if not instances:
        raise HTTPException(400, "No matching NetBox instances (check instance_ids/tags).")

    results = []
    for instance in instances:
        if instance.requires_approved_pr:
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
