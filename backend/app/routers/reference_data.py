from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import crypto, models
from app.auth import get_current_actor
from app.database import get_db
from app.rbac import AccessContext, filter_scoped, get_access_context, has_role_at_least, require_role, role_for_resource
from app.routers._type_common import get_target, github_error_to_http, log_action, resolve_instances, with_actor_trailer
from app.services import github_repo, netbox_client, reference_data

router = APIRouter(prefix="/api/repos/{target_id}/reference-data", tags=["reference-data"])


def _kind(kind: str):
    if kind not in reference_data.REGISTRY:
        raise HTTPException(404, "Unknown reference-data kind.")
    return kind


def _path(target, kind: str) -> str:
    try:
        return reference_data.path_for(target.reference_data_path, _kind(kind))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/schema")
def registry_schema(_: AccessContext = Depends(get_access_context)):
    return reference_data.schema()


@router.get("/{kind}/file")
def get_file(kind: str, target_id: str, db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    target = get_target(target_id, db, ctx)
    path, pat = _path(target, kind), crypto.decrypt(target.pat_encrypted)
    try:
        if not github_repo.file_exists(pat, target.repo, target.branch, path):
            return {"repo_target_id": target_id, "path": path, "exists": False, "sha": None, "payload": {"items": []}}
        branch = github_repo.resolve_working_branch(pat, target.repo, target.branch, path)
        result = github_repo.get_file(pat, target.repo, branch, path)
        payload = reference_data.validate_file(kind, result["payload"])
        return {"repo_target_id": target_id, "path": path, "exists": True, "sha": result["sha"], "payload": payload,
                "open_pr": github_repo.get_open_pr(pat, target.repo, target.branch, path)}
    except HTTPException:
        raise
    except Exception as exc:
        raise github_error_to_http(exc)


@router.put("/{kind}/file")
def save_file(kind: str, target_id: str, payload: dict, request: Request, db: Session = Depends(get_db),
              ctx: AccessContext = Depends(require_role("editor"))):
    target = get_target(target_id, db, ctx)
    path, pat = _path(target, kind), crypto.decrypt(target.pat_encrypted)
    try:
        validated = reference_data.validate_file(kind, payload.get("payload", {}))
        result = github_repo.save_file(
            pat, target.repo, target.branch, path, validated,
            payload.get("commit_message") or f"Update {kind} reference data",
            sha=payload.get("sha"), pr_body=with_actor_trailer(payload.get("pr_body"), get_current_actor(request)),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise github_error_to_http(exc)
    log_action(db, repo_target_id=target_id, file_path=path, target_name=target.name, status="success",
               detail=f"Updated {kind}, PR #{result['pr_number']}", actor=get_current_actor(request))
    return result


def _instance(instance_id: str, db: Session, ctx: AccessContext):
    instance = db.get(models.NetboxInstance, instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")
    return instance


@router.post("/{kind}/import-scan")
def import_scan(kind: str, target_id: str, payload: dict, db: Session = Depends(get_db),
                ctx: AccessContext = Depends(require_role("editor"))):
    target = get_target(target_id, db, ctx)
    instance = _instance(payload.get("instance_id", ""), db, ctx)
    token = crypto.decrypt(instance.api_token_encrypted)
    actual = reference_data.fetch_kind(instance.base_url, token, netbox_client.verify_for_instance(instance), _kind(kind))
    path, pat = _path(target, kind), crypto.decrypt(target.pat_encrypted)
    try:
        current = github_repo.get_file(pat, target.repo, target.branch, path)["payload"] if github_repo.file_exists(pat, target.repo, target.branch, path) else {"items": []}
        expected = reference_data.validate_file(kind, current)
    except Exception as exc:
        raise github_error_to_http(exc)
    return {"payload": actual, "diff": reference_data.diff_kind(expected, actual)}


@router.post("/{kind}/diff")
def diff(kind: str, target_id: str, payload: dict, db: Session = Depends(get_db),
         ctx: AccessContext = Depends(get_access_context)):
    target = get_target(target_id, db, ctx)
    path, pat = _path(target, kind), crypto.decrypt(target.pat_encrypted)
    try:
        expected = reference_data.validate_file(kind, github_repo.get_file(pat, target.repo, target.branch, path)["payload"])
    except Exception as exc:
        raise github_error_to_http(exc)
    results = []
    for instance in resolve_instances(db, payload.get("instance_ids", []), payload.get("tags", []), ctx):
        try:
            token = crypto.decrypt(instance.api_token_encrypted)
            actual = reference_data.fetch_kind(instance.base_url, token, netbox_client.verify_for_instance(instance), kind)
            results.append({"instance_id": instance.id, "instance_name": instance.name, "diff": reference_data.diff_kind(expected, actual)})
        except Exception as exc:
            results.append({"instance_id": instance.id, "instance_name": instance.name, "error": str(exc)})
    return results


@router.post("/push")
def push(target_id: str, payload: dict, request: Request, db: Session = Depends(get_db),
         ctx: AccessContext = Depends(require_role("editor"))):
    target = get_target(target_id, db, ctx)
    pat, files = crypto.decrypt(target.pat_encrypted), {}
    for kind in reference_data.PUSH_ORDER:
        path = _path(target, kind)
        try:
            files[kind] = github_repo.get_file(pat, target.repo, target.branch, path)["payload"] if github_repo.file_exists(pat, target.repo, target.branch, path) else {"items": []}
        except Exception as exc:
            raise github_error_to_http(exc)
    instances = resolve_instances(db, payload.get("instance_ids", []), payload.get("tags", []), ctx)
    if not instances:
        raise HTTPException(400, "No matching NetBox instances.")
    actor, results = get_current_actor(request), []
    for instance in instances:
        if not has_role_at_least(role_for_resource(ctx, "instance", instance.id), "admin"):
            results.append({"target": instance.name, "status": "error", "detail": "Instance admin role required."})
            continue
        if instance.requires_approved_pr:
            blocked = None
            for kind in reference_data.PUSH_ORDER:
                try:
                    approval = github_repo.get_merged_pr_approval(pat, target.repo, target.branch, _path(target, kind))
                    if files[kind].get("items") and (not approval["found"] or not approval["approved"]):
                        blocked = f"No approved, merged PR found for {kind}."
                        break
                except Exception as exc:
                    blocked = f"Could not verify approved PR: {exc}"
                    break
            if blocked:
                results.append({"target": instance.name, "status": "error", "detail": blocked})
                continue
        try:
            token = crypto.decrypt(instance.api_token_encrypted)
            outcome = reference_data.push_all(instance.base_url, token, netbox_client.verify_for_instance(instance), files,
                                              overwrite=bool(payload.get("overwrite")),
                                              enable_event_rules=bool(payload.get("enable_event_rules")))
            detail = f"Created {len(outcome['created'])}, updated {len(outcome['updated'])}, skipped {len(outcome['skipped'])}, failed {len(outcome['failed'])}."
            results.append({"target": instance.name, "status": outcome["status"], "detail": detail, "warnings": outcome["warnings"]})
            log_action(db, repo_target_id=target_id, file_path=target.reference_data_path, target_name=instance.name,
                       status=outcome["status"], detail=detail, actor=actor, action_type="netbox")
        except Exception as exc:
            results.append({"target": instance.name, "status": "error", "detail": str(exc)})
    return results
