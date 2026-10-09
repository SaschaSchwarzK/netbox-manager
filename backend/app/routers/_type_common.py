import base64
import binascii

import requests
from fastapi import HTTPException
from github import GithubException
from sqlalchemy.orm import Session

from app import models, schemas
from app.image_safety import MAX_IMAGE_BASE64_CHARS, validate_image_bytes
from app.rbac import AccessContext, filter_scoped, require_resource_role, require_visible
from app.services.github_repo import ArchiveBusyError, RepoAccessError

_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}


def decode_editor_image(payload: schemas.ImageFileRequest) -> tuple[bytes, str]:
    extension = payload.filename.rsplit(".", 1)[-1].lower() if "." in payload.filename else ""
    if extension not in _IMAGE_EXTENSIONS or not payload.content_type.startswith("image/"):
        raise HTTPException(422, "Images must be PNG, JPEG, or WebP files.")
    if len(payload.content_base64) > MAX_IMAGE_BASE64_CHARS:
        raise HTTPException(422, "Image must be no larger than 10 MiB.")
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "Image content is not valid base64.") from exc
    try:
        return content, validate_image_bytes(content, extension)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def get_target(target_id: str, db: Session, ctx: AccessContext | None = None,
               minimum_role: str | None = None) -> models.GithubTarget:
    target = db.get(models.GithubTarget, target_id)
    if not target:
        raise HTTPException(404, "GitHub target not found.")
    if ctx is not None:
        effective_minimum = minimum_role or ctx.required_role
        if effective_minimum:
            require_resource_role(ctx, "github_target", target_id, effective_minimum, db)
        else:
            require_visible("github_target", target_id, ctx, db)
    return target


def github_error_to_http(exc: Exception) -> HTTPException:
    if isinstance(exc, ArchiveBusyError):
        return HTTPException(503, str(exc), headers={"Retry-After": "5"})
    if isinstance(exc, RepoAccessError):
        return HTTPException(404, str(exc))
    if isinstance(exc, GithubException):
        detail = exc.data.get("message", str(exc)) if isinstance(exc.data, dict) else str(exc)
        return HTTPException(exc.status if isinstance(exc.status, int) else 502, f"GitHub API error: {detail}")
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        return HTTPException(status if status in (401, 403, 404) else 502,
                             f"GitHub archive request failed: HTTP {status}")
    return HTTPException(500, str(exc))


def with_actor_trailer(pr_body: str | None, actor: dict) -> str:
    who = actor["name"] or "anonymous"
    if actor.get("email"):
        who = f"{who} <{actor['email']}>"
    username_line = f"\nOIDC username: {actor['username']}" if actor.get("username") else ""
    return (pr_body or "").rstrip() + f"\n\n---\nRequested via NetBox Manager by: {who}{username_line}"


def log_action(db: Session, *, repo_target_id: str, file_path: str, target_name: str,
               status: str, detail: str | None, actor: dict, action_type: str = "github") -> None:
    db.add(models.DeviceTypePushHistory(
        repo_target_id=repo_target_id, file_path=file_path, target_type=action_type,
        target_name=target_name, status=status, detail=detail, actor_sub=actor.get("sub"),
        actor_name=actor.get("name"), actor_email=actor.get("email"),
    ))
    db.commit()


def resolve_instances(db: Session, instance_ids: list[str], tags: list[str],
                      ctx: AccessContext | None = None) -> list[models.NetboxInstance]:
    wanted_ids, wanted_tags = set(instance_ids), set(tags)
    if not wanted_ids and not wanted_tags:
        return []
    instances = db.query(models.NetboxInstance).all()
    if ctx is not None:
        instances = filter_scoped(instances, "instance", ctx, db)
    return list({item.id: item for item in instances
                 if item.id in wanted_ids or wanted_tags.intersection(item.tags)}.values())
