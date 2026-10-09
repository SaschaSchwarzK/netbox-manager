from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.auth import get_current_actor
from app.customfield_schema import CustomFieldsTemplate
from app.database import get_db
from app.config import settings
from app.path_safety import validate_repo_path
from app.rbac import AccessContext, filter_scoped, get_access_context, has_role_at_least, require_role, role_for_resource
from app.routers.device_types import _get_target, _github_error_to_http, _log_action, _resolve_instances, _with_actor_trailer
from app.services import diff as diff_mod
from app.services import github_repo, netbox_client, netbox_customfields

router = APIRouter(prefix="/api/repos/{target_id}/custom-fields", tags=["custom-fields"])
_scope_serializer = URLSafeTimedSerializer(settings.session_secret_key, salt="custom-field-scope-reduction")
_SCOPE_TOKEN_MAX_AGE = 30 * 60


def _template_path(target: models.GithubTarget) -> str:
    try:
        return validate_repo_path(target.custom_fields_path, "custom-fields", (".yml", ".yaml"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _normalized_template(payload: dict) -> dict:
    """Accept legacy repository YAML and expose one canonical API shape."""
    return CustomFieldsTemplate(**payload).to_yaml_dict()


def _require_instance_admin(instances: list[models.NetboxInstance], ctx: AccessContext) -> None:
    denied = [instance.name for instance in instances if not has_role_at_least(
        role_for_resource(ctx, "instance", instance.id), "admin"
    )]
    if denied:
        raise HTTPException(403, f"Instance admin role required for: {', '.join(denied)}")


def _scope_preview_for_instance(instance, token: str, template: dict, actor: dict, include_backup: bool) -> dict:
    preview = netbox_customfields.preview_scope_reductions(
        instance.base_url, token, netbox_client.verify_for_instance(instance), template,
        instance={"id": instance.id, "name": instance.name, "base_url": instance.base_url},
        actor=actor, include_backup=include_backup,
    )
    if preview["reductions"]:
        preview["confirmation_token"] = _scope_serializer.dumps({
            "instance_id": instance.id,
            "fingerprint": netbox_customfields.scope_preview_fingerprint(preview),
            "backup_requested": include_backup,
        })
    else:
        preview["confirmation_token"] = None
    return preview


def _validate_scope_confirmation(instance_id: str, preview: dict, confirmation) -> dict:
    try:
        signed = _scope_serializer.loads(confirmation.token, max_age=_SCOPE_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise ValueError("Scope-reduction confirmation expired or is invalid; run preview again.")
    if signed.get("instance_id") != instance_id or signed.get("fingerprint") != netbox_customfields.scope_preview_fingerprint(preview):
        raise ValueError("Scope-reduction counts changed since preview; run preview again.")
    if confirmation.typed_field_names != preview["confirmation_text"]:
        raise ValueError(f"Type {preview['confirmation_text']!r} exactly to confirm the scope reduction.")
    if signed.get("backup_requested"):
        if not confirmation.backup_acknowledged:
            raise ValueError("Download and acknowledge the backup before applying.")
    elif confirmation.backup_opt_out_confirmation != f"NO BACKUP {preview['confirmation_text']}":
        raise ValueError(f"Type 'NO BACKUP {preview['confirmation_text']}' exactly to apply without a backup.")
    return signed


@router.get("/file", response_model=schemas.CustomFieldsTemplateOut)
def get_template(target_id: str, db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    target = _get_target(target_id, db, ctx)
    pat = crypto.decrypt(target.pat_encrypted)
    path = _template_path(target)

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

    try:
        normalized = _normalized_template(result["payload"])
    except Exception as exc:
        raise HTTPException(422, f"Repository custom-fields template is invalid: {exc}")
    return schemas.CustomFieldsTemplateOut(
        repo_target_id=target_id, path=path, exists=True, sha=result["sha"], payload=normalized, open_pr=open_pr
    )


@router.put("/file", response_model=schemas.SaveResult)
def save_template(
    target_id: str, payload: schemas.SaveCustomFieldsRequest, request: Request, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    target = _get_target(target_id, db, ctx)
    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)
    path = _template_path(target)

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


@router.post("/import-scan", response_model=schemas.CustomFieldsImportScanResult)
def import_scan(
    target_id: str, payload: schemas.CustomFieldsImportScanRequest, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    """
    Compares a NetBox instance's custom fields/choice sets against the current
    template and returns only what's missing from the template or differs from
    it — the set of things worth reviewing for import, not everything on the
    instance. Reuses the same diff engine the drift check uses, just reading
    its "extra_on_instance" (not in template) and "changed" lists as import
    candidates instead of as drift to flag.
    """
    target = _get_target(target_id, db, ctx)
    instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")

    pat = crypto.decrypt(target.pat_encrypted)
    path = _template_path(target)
    try:
        template_exists = github_repo.file_exists(pat, target.repo, target.branch, path)
        template = _normalized_template(github_repo.get_file(pat, target.repo, target.branch, path)["payload"]) if template_exists else CustomFieldsTemplate().to_yaml_dict()
    except Exception as exc:
        raise _github_error_to_http(exc)

    token = crypto.decrypt(instance.api_token_encrypted)
    try:
        fetched = netbox_customfields.get_existing_custom_fields(instance.base_url, token, netbox_client.verify_for_instance(instance))
    except Exception as exc:
        raise HTTPException(502, f"Could not read custom fields from {instance.name}: {exc}")

    diff_result = diff_mod.diff_custom_fields_template(template, fetched)
    instance_cf_by_name = {f["name"]: f for f in fetched.get("custom_fields", []) if f.get("name")}
    instance_cs_by_name = {c["name"]: c for c in fetched.get("custom_field_choice_sets", []) if c.get("name")}

    candidates = []
    cf_diff = diff_result["custom_fields"] or {"extra_on_instance": [], "changed": []}
    for name in cf_diff["extra_on_instance"]:
        candidates.append(schemas.ImportCandidate(kind="custom_field", name=name, status="missing", payload=instance_cf_by_name[name]))
    for item in cf_diff["changed"]:
        candidates.append(schemas.ImportCandidate(
            kind="custom_field", name=item["name"], status="changed",
            payload=instance_cf_by_name[item["name"]], field_changes=item["field_changes"],
        ))

    cs_diff = diff_result["custom_field_choice_sets"] or {"extra_on_instance": [], "changed": []}
    for name in cs_diff["extra_on_instance"]:
        candidates.append(schemas.ImportCandidate(kind="choice_set", name=name, status="missing", payload=instance_cs_by_name[name]))
    for item in cs_diff["changed"]:
        candidates.append(schemas.ImportCandidate(
            kind="choice_set", name=item["name"], status="changed",
            payload=instance_cs_by_name[item["name"]], field_changes=item["field_changes"],
        ))

    return schemas.CustomFieldsImportScanResult(template_exists=template_exists, candidates=candidates)


@router.post("/import", response_model=schemas.SaveResult, status_code=201)
def import_from_instance(
    target_id: str, payload: schemas.ImportCustomFieldsSelectionRequest, request: Request, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    """
    Merges only the selected custom fields/choice sets into the existing
    template (added if missing, replaced if they differ) and saves that as a
    PR — everything not selected is left exactly as it is in the template.
    Both the template and the instance's current state are re-fetched fresh
    here rather than trusting whatever the scan step returned, since either
    could have changed between scanning and importing.
    """
    target = _get_target(target_id, db, ctx)
    instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")
    if not payload.selected:
        raise HTTPException(400, "No custom fields or choice sets selected to import.")

    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)
    path = _template_path(target)

    try:
        template_exists = github_repo.file_exists(pat, target.repo, target.branch, path)
        current = github_repo.get_file(pat, target.repo, target.branch, path) if template_exists else None
    except Exception as exc:
        raise _github_error_to_http(exc)
    template = _normalized_template(current["payload"]) if current else CustomFieldsTemplate().to_yaml_dict()
    sha = current["sha"] if current else None

    token = crypto.decrypt(instance.api_token_encrypted)
    try:
        fetched = netbox_customfields.get_existing_custom_fields(instance.base_url, token, netbox_client.verify_for_instance(instance))
    except Exception as exc:
        raise HTTPException(502, f"Could not read custom fields from {instance.name}: {exc}")

    instance_cf_by_name = {f["name"]: f for f in fetched.get("custom_fields", []) if f.get("name")}
    instance_cs_by_name = {c["name"]: c for c in fetched.get("custom_field_choice_sets", []) if c.get("name")}
    template_cf_by_name = {f["name"]: f for f in template.get("custom_fields", []) if f.get("name")}
    template_cs_by_name = {c["name"]: c for c in template.get("custom_field_choice_sets", []) if c.get("name")}

    imported, no_longer_on_instance = [], []
    for sel in payload.selected:
        source_map = instance_cf_by_name if sel.kind == "custom_field" else instance_cs_by_name
        dest_map = template_cf_by_name if sel.kind == "custom_field" else template_cs_by_name
        item = source_map.get(sel.name)
        if item is None:
            no_longer_on_instance.append(f"{sel.kind}:{sel.name}")
            continue
        dest_map[sel.name] = item
        imported.append(f"{sel.kind}:{sel.name}")

    if not imported:
        raise HTTPException(400, "None of the selected items were still found on the instance; nothing to import.")

    merged = {"custom_fields": list(template_cf_by_name.values()), "custom_field_choice_sets": list(template_cs_by_name.values())}
    try:
        validated = CustomFieldsTemplate(**merged)
    except Exception as exc:
        raise HTTPException(422, f"Merged template failed validation: {exc}")

    commit_message = payload.commit_message or f"Import {len(imported)} item(s) from {instance.name} into custom-fields template"
    pr_body_lines = [f"Imports the following from **{instance.name}** into the custom-fields template:", "", *[f"- {i}" for i in imported]]
    if no_longer_on_instance:
        pr_body_lines += ["", f"Skipped (no longer found on the instance): {', '.join(no_longer_on_instance)}"]
    pr_body = payload.pr_body or "\n".join(pr_body_lines)

    try:
        result = github_repo.save_file(
            pat, target.repo, target.branch, path, validated.to_yaml_dict(), commit_message,
            sha=sha, pr_body=_with_actor_trailer(pr_body, actor),
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
                status="success", detail=f"Imported {len(imported)} item(s) from {instance.name}, PR #{result['pr_number']}", actor=actor)
    return schemas.SaveResult(**result)


@router.post("/push/preview")
def preview_push_to_instances(
    target_id: str, payload: schemas.PreviewCustomFieldsPushRequest, request: Request,
    db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor")),
):
    target = _get_target(target_id, db, ctx)
    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)
    path = _template_path(target)
    try:
        template = _normalized_template(github_repo.get_file(pat, target.repo, target.branch, path)["payload"])
    except Exception as exc:
        raise _github_error_to_http(exc)
    instances = _resolve_instances(db, payload.instance_ids, payload.tags, ctx)
    if not instances:
        raise HTTPException(400, "No matching NetBox instances (check instance_ids/tags).")
    results = []
    for instance in instances:
        if not has_role_at_least(role_for_resource(ctx, "instance", instance.id), "editor"):
            results.append(schemas.PushResultItem(
                target=instance.name, status="error",
                detail="Blocked: editor role is required on this instance.",
            ))
            continue
        token = crypto.decrypt(instance.api_token_encrypted)
        try:
            preview = _scope_preview_for_instance(
                instance, token, template, actor, payload.include_backup and payload.overwrite
            ) if payload.overwrite else {
                "netbox_version": None, "reductions": [], "confirmation_text": "",
                "backup_possible": True, "backup": None, "confirmation_token": None,
            }
            results.append({"instance_id": instance.id, "instance_name": instance.name, **preview})
            counts = sum(row["meaningful_count"] for item in preview["reductions"] for row in item["object_types"])
            _log_action(
                db, repo_target_id=target_id, file_path=path, target_name=instance.name,
                status="success", detail=f"Scope-reduction preview: {len(preview['reductions'])} field(s), {counts} meaningful value(s); backup={'yes' if preview['backup'] else 'no'}.", actor=actor,
                action_type="netbox",
            )
        except Exception as exc:
            results.append({"instance_id": instance.id, "instance_name": instance.name, "error": str(exc)})
            _log_action(db, repo_target_id=target_id, file_path=path, target_name=instance.name,
                        status="error", detail=f"Scope-reduction preview failed: {exc}", actor=actor,
                        action_type="netbox")
    return results


@router.post("/restore")
def restore_custom_fields(
    target_id: str, payload: schemas.RestoreCustomFieldsRequest, request: Request,
    db: Session = Depends(get_db), ctx: AccessContext = Depends(require_role("editor")),
):
    target = _get_target(target_id, db, ctx)
    instance = db.get(models.NetboxInstance, payload.instance_id)
    if not instance or not filter_scoped([instance], "instance", ctx, db):
        raise HTTPException(404, "NetBox instance not found.")
    _require_instance_admin([instance], ctx)
    backup_instance = payload.backup.get("instance") or {}
    if str(backup_instance.get("id")) != str(instance.id) or backup_instance.get("base_url", "").rstrip("/") != instance.base_url.rstrip("/"):
        raise HTTPException(400, "Backup belongs to a different NetBox instance.")
    actor = get_current_actor(request)
    token = crypto.decrypt(instance.api_token_encrypted)
    try:
        result = netbox_customfields.restore_custom_fields_backup(
            instance.base_url, token, netbox_client.verify_for_instance(instance), payload.backup, dry_run=payload.dry_run
        )
    except Exception as exc:
        _log_action(db, repo_target_id=target_id, file_path=target.custom_fields_path, target_name=instance.name,
                    status="error", detail=f"Custom-field restore failed: {exc}", actor=actor,
                    action_type="netbox")
        raise HTTPException(400, str(exc))
    errors = sum(item["status"] == "error" for item in result["results"])
    _log_action(db, repo_target_id=target_id, file_path=target.custom_fields_path, target_name=instance.name,
                status="error" if errors else "success",
                detail=f"Custom-field restore {'dry run' if payload.dry_run else 'apply'}: {len(result['results'])} record(s), {errors} error(s).", actor=actor,
                action_type="netbox")
    return result


@router.post("/push", response_model=list[schemas.PushResultItem])
def push_to_instances(
    target_id: str, payload: schemas.PushCustomFieldsRequest, request: Request, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    target = _get_target(target_id, db, ctx)
    pat = crypto.decrypt(target.pat_encrypted)
    actor = get_current_actor(request)
    path = _template_path(target)

    try:
        template = _normalized_template(github_repo.get_file(pat, target.repo, target.branch, path)["payload"])
    except Exception as exc:
        raise _github_error_to_http(exc)

    instances = _resolve_instances(db, payload.instance_ids, payload.tags, ctx)
    if not instances:
        raise HTTPException(400, "No matching NetBox instances (check instance_ids/tags).")
    _require_instance_admin(instances, ctx)

    results = []
    for instance in instances:
        if not has_role_at_least(role_for_resource(ctx, "instance", instance.id), "editor"):
            results.append(schemas.PushResultItem(
                target=instance.name, status="error",
                detail="Blocked: editor role is required on this instance.",
            ))
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
            scope_reductions_confirmed = False
            if payload.overwrite:
                preview = _scope_preview_for_instance(instance, token, template, actor, include_backup=False)
                if preview["reductions"]:
                    confirmation = payload.confirmations.get(instance.id)
                    if not confirmation:
                        raise ValueError("Scope reduction detected. Preview and confirm this instance before applying.")
                    signed = _validate_scope_confirmation(instance.id, preview, confirmation)
                    scope_reductions_confirmed = True
                    _log_action(db, repo_target_id=target_id, file_path=path, target_name=instance.name,
                                status="success", detail=f"Confirmed scope reduction for {preview['confirmation_text']}; backup={'yes' if signed.get('backup_requested') else 'opted out'}.", actor=actor,
                                action_type="netbox")
            outcome = netbox_customfields.push_custom_fields(
                instance.base_url, token, netbox_client.verify_for_instance(instance), template, payload.overwrite,
                scope_reductions_confirmed=scope_reductions_confirmed,
                instance_name=instance.name,
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


@router.post("/diff", response_model=list[schemas.InstanceCustomFieldsDiffResult])
def diff_with_instances(
    target_id: str, payload: schemas.PushCustomFieldsRequest, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("editor")),
):
    """
    Drift check: for each selected instance, what's in the template but
    missing there, what's there but not in the template, and what's changed.
    """
    target = _get_target(target_id, db, ctx)
    pat = crypto.decrypt(target.pat_encrypted)
    path = _template_path(target)
    try:
        template = _normalized_template(github_repo.get_file(pat, target.repo, target.branch, path)["payload"])
    except Exception as exc:
        raise _github_error_to_http(exc)

    instances = _resolve_instances(db, payload.instance_ids, payload.tags, ctx)
    if not instances:
        raise HTTPException(400, "No matching NetBox instances (check instance_ids/tags).")

    def check_one(instance: models.NetboxInstance) -> schemas.InstanceCustomFieldsDiffResult:
        token = crypto.decrypt(instance.api_token_encrypted)
        try:
            existing = netbox_customfields.get_existing_custom_fields(instance.base_url, token, netbox_client.verify_for_instance(instance))
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
