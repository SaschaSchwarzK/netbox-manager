from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.auth import get_current_actor
from app.customfield_schema import CustomFieldsTemplate
from app.database import get_db
from app.routers.device_types import _get_target, _github_error_to_http, _log_action, _resolve_instances, _with_actor_trailer
from app.services import diff as diff_mod
from app.services import github_repo, netbox_customfields

router = APIRouter(prefix="/api/repos/{target_id}/custom-fields", tags=["custom-fields"])


@router.get("/file", response_model=schemas.CustomFieldsTemplateOut)
def get_template(target_id: str, db: Session = Depends(get_db)):
    target = _get_target(target_id, db)
    pat = crypto.decrypt(target.pat_encrypted)
    path = target.custom_fields_path

    try:
        exists = github_repo.file_exists(pat, target.repo, target.branch, path)
    except Exception as exc:
        raise _github_error_to_http(exc)

    if not exists:
        empty = CustomFieldsTemplate().to_yaml_dict()
        return schemas.CustomFieldsTemplateOut(repo_target_id=target_id, path=path, exists=False, sha=None, payload=empty)

    try:
        working_branch = github_repo.resolve_working_branch(pat, target.repo, target.branch, path)
        result = github_repo.get_file(pat, target.repo, working_branch, path)
        open_pr = github_repo.get_open_pr(pat, target.repo, target.branch, path)
    except Exception as exc:
        raise _github_error_to_http(exc)

    return schemas.CustomFieldsTemplateOut(
        repo_target_id=target_id, path=path, exists=True, sha=result["sha"], payload=result["payload"], open_pr=open_pr
    )


@router.put("/file", response_model=schemas.SaveResult)
def save_template(target_id: str, payload: schemas.SaveCustomFieldsRequest, request: Request, db: Session = Depends(get_db)):
    target = _get_target(target_id, db)
    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)
    path = target.custom_fields_path

    try:
        validated = CustomFieldsTemplate(**payload.payload)
    except Exception as exc:
        raise HTTPException(422, f"Payload does not match custom-fields template schema: {exc}")

    field_count = len(validated.custom_fields)
    choice_set_count = len(validated.custom_field_choice_sets)
    commit_message = payload.commit_message or f"Update custom-fields template ({field_count} fields, {choice_set_count} choice sets)"

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


@router.post("/import", response_model=schemas.SaveResult, status_code=201)
def import_from_instance(target_id: str, payload: schemas.ImportCustomFieldsRequest, request: Request, db: Session = Depends(get_db)):
    """Pulls the current custom fields/choice sets from a NetBox instance and saves them as the new template, via PR."""
    target = _get_target(target_id, db)
    instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance:
        raise HTTPException(404, "NetBox instance not found.")

    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)
    path = target.custom_fields_path

    token = crypto.decrypt(instance.api_token_encrypted)
    try:
        fetched = netbox_customfields.get_existing_custom_fields(instance.base_url, token, instance.verify_ssl)
    except Exception as exc:
        raise HTTPException(502, f"Could not read custom fields from {instance.name}: {exc}")

    validated = CustomFieldsTemplate(**fetched)
    commit_message = payload.commit_message or f"Import custom-fields template from {instance.name}"
    pr_body = payload.pr_body or (
        f"Imports the current custom fields and choice sets from **{instance.name}** as the new template "
        f"({len(validated.custom_fields)} fields, {len(validated.custom_field_choice_sets)} choice sets)."
    )

    try:
        result = github_repo.save_file(
            pat, target.repo, target.branch, path, validated.to_yaml_dict(), commit_message,
            sha=payload.sha, pr_body=_with_actor_trailer(pr_body, actor),
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
                status="success", detail=f"Imported from {instance.name}, PR #{result['pr_number']}", actor=actor)
    return schemas.SaveResult(**result)


@router.post("/push", response_model=list[schemas.PushResultItem])
def push_to_instances(target_id: str, payload: schemas.PushCustomFieldsRequest, request: Request, db: Session = Depends(get_db)):
    target = _get_target(target_id, db)
    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)
    path = target.custom_fields_path

    try:
        template = github_repo.get_file(pat, target.repo, target.branch, path)["payload"]
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
                approval = None
                detail = f"Could not verify PR approval (blocked, this instance requires it): {exc}"
            else:
                detail = None
                if not approval["found"]:
                    detail = "Blocked: this instance requires an approved, merged PR, but none was found for the current template."
                elif not approval["approved"]:
                    detail = f"Blocked: PR #{approval['pr_number']} merged this change but has no approving review."
            if detail:
                results.append(schemas.PushResultItem(target=instance.name, status="error", detail=detail))
                db.add(models.DeviceTypePushHistory(
                    repo_target_id=target_id, file_path=path, target_type="netbox", target_name=instance.name,
                    status="error", detail=detail,
                    actor_sub=actor.get("sub"), actor_name=actor.get("name"), actor_email=actor.get("email"),
                ))
                continue

        token = crypto.decrypt(instance.api_token_encrypted)
        try:
            outcome = netbox_customfields.push_custom_fields(instance.base_url, token, instance.verify_ssl, template, payload.overwrite)
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


@router.post("/diff", response_model=list[schemas.InstanceCustomFieldsDiffResult])
def diff_with_instances(target_id: str, payload: schemas.PushCustomFieldsRequest, db: Session = Depends(get_db)):
    """
    Drift check: for each selected instance, what's in the template but
    missing there, what's there but not in the template, and what's changed.
    """
    target = _get_target(target_id, db)
    pat = crypto.decrypt(target.pat_encrypted)
    path = target.custom_fields_path
    try:
        template = github_repo.get_file(pat, target.repo, target.branch, path)["payload"]
    except Exception as exc:
        raise _github_error_to_http(exc)

    instances = _resolve_instances(db, payload.instance_ids, payload.tags)
    if not instances:
        raise HTTPException(400, "No matching NetBox instances (check instance_ids/tags).")

    def check_one(instance: models.NetboxInstance) -> schemas.InstanceCustomFieldsDiffResult:
        token = crypto.decrypt(instance.api_token_encrypted)
        try:
            existing = netbox_customfields.get_existing_custom_fields(instance.base_url, token, instance.verify_ssl)
            result = diff_mod.diff_custom_fields_template(template, existing)
            return schemas.InstanceCustomFieldsDiffResult(instance_id=instance.id, instance_name=instance.name, diff=result)
        except Exception as exc:
            return schemas.InstanceCustomFieldsDiffResult(instance_id=instance.id, instance_name=instance.name, error=str(exc))

    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(check_one, inst) for inst in instances]
        for future in as_completed(futures):
            results.append(future.result())

    order = {inst.id: idx for idx, inst in enumerate(instances)}
    results.sort(key=lambda r: order.get(r.instance_id, 0))
    return results
