"""
Two independent, deliberately simple mappings:

1. Role (group -> viewer/editor/admin, highest wins across a user's groups).
   Gates *actions* — see require_role().
2. Scope (group -> specific instance/GitHub target it can see). Gates
   *visibility* — a resource with zero scope mappings is visible to everyone
   (opt-in restriction, not opt-out), so shipping this feature never locks
   existing users out of existing resources by default.

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


def resolve_role(groups: list[str], db: Session) -> str:
    bootstrap_groups = {g.strip() for g in settings.bootstrap_admin_groups.split(",") if g.strip()}
    if bootstrap_groups & set(groups):
        return "admin"
    if not groups:
        return settings.default_role
    mappings = db.query(models.RoleMapping).filter(models.RoleMapping.oidc_group.in_(groups)).all()
    if not mappings:
        return settings.default_role
    best = max(mappings, key=lambda m: ROLE_ORDER.get(m.role, 0))
    return best.role


def has_role_at_least(role: str, minimum: str) -> bool:
    return ROLE_ORDER.get(role, -1) >= ROLE_ORDER.get(minimum, 99)


@dataclass
class AccessContext:
    role: str
    groups: list[str] = field(default_factory=list)
    scoping_active: bool = True  # False when OIDC is off entirely; scope checks are skipped in that case


def get_access_context(request: Request, db: Session = Depends(get_db)) -> AccessContext:
    if not oauth:
        return AccessContext(role="admin", groups=[], scoping_active=False)
    user = get_current_user_optional(request)
    groups = (user or {}).get("groups", []) or []
    return AccessContext(role=resolve_role(groups, db), groups=groups, scoping_active=True)


def require_role(minimum: str):
    """FastAPI dependency factory: Depends(require_role("editor")) etc."""
    def dependency(ctx: AccessContext = Depends(get_access_context)) -> AccessContext:
        if not has_role_at_least(ctx.role, minimum):
            raise HTTPException(403, f"This action requires the '{minimum}' role or higher; you have '{ctx.role}'.")
        return ctx
    return dependency


def scoped_resource_ids(db: Session, resource_type: str) -> dict[str, set[str]]:
    """resource_id -> set of oidc_groups allowed to see it, for every resource of this type that has ANY scoping at all."""
    mappings = db.query(models.ScopeMapping).filter_by(resource_type=resource_type).all()
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
