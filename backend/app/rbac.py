"""Unified role and scope grants.

One access-mapping row maps an OIDC group to a role and a scope. The "*"/"*"
scope applies to all resources, while a NULL role makes a row visibility-only.
Visibility remains opt-in per resource: a resource with no concrete mappings is
visible to everyone, and a global mapping does not reveal specifically scoped
resources.

When OIDC isn't configured at all (local/dev), every request gets role
"admin" and scoping is skipped entirely — consistent with how auth itself
already behaves in that mode elsewhere in the app.
"""
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user_optional, oauth
from app.config import settings
from app.database import get_db

ROLE_ORDER = {"viewer": 0, "editor": 1, "admin": 2}
ROLES = tuple(ROLE_ORDER.keys())


def _bootstrap_admin(groups: list[str]) -> bool:
    bootstrap_groups = {g.strip() for g in settings.bootstrap_admin_groups.split(",") if g.strip()}
    return bool(bootstrap_groups & set(groups))


def _role_from_mappings(groups: list[str], mappings: list[models.AccessMapping]) -> str:
    if _bootstrap_admin(groups):
        return "admin"
    roles = [settings.default_role]
    roles.extend(mapping.role for mapping in mappings if mapping.role is not None)
    return max(roles, key=lambda role: ROLE_ORDER.get(role, -1))


def resolve_role(groups: list[str], db: Session) -> str:
    mappings = []
    if groups:
        mappings = (
            db.query(models.AccessMapping)
            .filter(models.AccessMapping.oidc_group.in_(groups))
            .all()
        )
    return _role_from_mappings(groups, mappings)


def has_role_at_least(role: str, minimum: str) -> bool:
    return ROLE_ORDER.get(role, -1) >= ROLE_ORDER.get(minimum, 99)


@dataclass
class AccessContext:
    role: str
    groups: list[str] = field(default_factory=list)
    scoping_active: bool = True  # False when OIDC is off entirely; scope checks are skipped in that case
    mappings: list[models.AccessMapping] = field(default_factory=list)
    app_admin: bool = True


def get_access_context(request: Request, db: Session = Depends(get_db)) -> AccessContext:
    user = get_current_user_optional(request)
    if (user or {}).get("local") is True:
        return AccessContext(role="admin", groups=[], scoping_active=False, app_admin=True)
    if not settings.auth_required:
        return AccessContext(role="admin", groups=[], scoping_active=False)
    groups = (user or {}).get("groups", []) or []
    mappings = []
    if groups:
        mappings = (
            db.query(models.AccessMapping)
            .filter(models.AccessMapping.oidc_group.in_(groups))
            .all()
        )
    return AccessContext(
        role=_role_from_mappings(groups, mappings),
        groups=groups,
        scoping_active=True,
        mappings=mappings,
        app_admin=_bootstrap_admin(groups) or any(
            mapping.role == "admin"
            and mapping.resource_type == models.SCOPE_ALL
            and mapping.resource_id == models.SCOPE_ALL
            for mapping in mappings
        ),
    )


def role_for_resource(ctx: AccessContext, resource_type: str, resource_id: str) -> str:
    if ctx.app_admin:
        return ctx.role
    best = settings.default_role
    for mapping in ctx.mappings:
        if mapping.role is None:
            continue
        scope = (mapping.resource_type, mapping.resource_id)
        if scope == (models.SCOPE_ALL, models.SCOPE_ALL) or scope == (
            resource_type,
            resource_id,
        ):
            if ROLE_ORDER.get(mapping.role, -1) > ROLE_ORDER.get(best, -1):
                best = mapping.role
    return best


def require_role(minimum: str):
    """FastAPI dependency factory: Depends(require_role("editor")) etc."""
    def dependency(ctx: AccessContext = Depends(get_access_context)) -> AccessContext:
        if not has_role_at_least(ctx.role, minimum):
            raise HTTPException(403, f"This action requires the '{minimum}' role or higher; you have '{ctx.role}'.")
        return ctx
    return dependency


def is_app_admin(ctx: AccessContext) -> bool:
    return ctx.app_admin


def require_app_admin():
    def dependency(ctx: AccessContext = Depends(get_access_context)) -> AccessContext:
        if not ctx.app_admin:
            raise HTTPException(
                403,
                "Managing NetBox Manager itself requires app-level admin — a mapping with role "
                "'admin' on 'All resources', or NBM_BOOTSTRAP_ADMIN_GROUPS.",
            )
        return ctx
    return dependency


def require_visible(
    resource_type: str,
    resource_id: str,
    ctx: AccessContext,
    db: Session,
) -> None:
    if resource_type == "instance":
        model = models.NetboxInstance
        message = "Instance not found."
    else:
        model = models.GithubTarget
        message = "GitHub target not found."
    resource = db.get(model, resource_id)
    if resource is None or filter_scoped([resource], resource_type, ctx, db) == []:
        raise HTTPException(404, message)


def require_resource_admin(
    ctx: AccessContext,
    resource_type: str,
    resource_id: str,
    db: Session,
) -> None:
    require_visible(resource_type, resource_id, ctx, db)
    if ctx.app_admin:
        return
    if not has_role_at_least(role_for_resource(ctx, resource_type, resource_id), "admin"):
        raise HTTPException(
            403,
            f"Requires app-level admin, or the admin role mapped to this {resource_type}.",
        )


def scoped_resource_ids(db: Session, resource_type: str) -> dict[str, set[str]]:
    """resource_id -> set of oidc_groups allowed to see it, for every resource of this type that has ANY scoping at all."""
    mappings = (
        db.query(models.AccessMapping)
        .filter(
            models.AccessMapping.resource_type == resource_type,
            models.AccessMapping.resource_id != models.SCOPE_ALL,
        )
        .all()
    )
    result: dict[str, set[str]] = {}
    for m in mappings:
        result.setdefault(m.resource_id, set()).add(m.oidc_group)
    return result


def can_access(resource_id: str, allowed_by_resource: dict[str, set[str]], groups: list[str]) -> bool:
    allowed = allowed_by_resource.get(resource_id)
    if allowed is None:
        return True  # no scoping configured for this resource at all -> visible to everyone
    return bool(allowed & set(groups))


def filter_scoped(items: list, resource_type: str, ctx: AccessContext, db: Session, id_attr: str = "id") -> list:
    """Filters a list of ORM objects (or anything with an `id_attr` attribute) down to what ctx can see."""
    if not ctx.scoping_active:
        return items
    allowed_by_resource = scoped_resource_ids(db, resource_type)
    if not allowed_by_resource:
        return items  # nothing at all is scoped for this resource type -> skip the per-item check entirely
    return [item for item in items if can_access(getattr(item, id_attr), allowed_by_resource, ctx.groups)]
