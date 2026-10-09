from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db
from app.rbac import AccessContext, ROLES, filter_scoped, require_app_admin, require_role

router = APIRouter(prefix="/api/access", tags=["access"])


@router.get("/me", response_model=schemas.CurrentAccessOut)
def current_access(
    db: Session = Depends(get_db),
    ctx: AccessContext = Depends(require_role("viewer")),
):
    editable = {"instance": [], "github_target": []}
    if ctx.app_admin:
        instances = filter_scoped(db.query(models.NetboxInstance).all(), "instance", ctx, db)
        targets = filter_scoped(db.query(models.GithubTarget).all(), "github_target", ctx, db)
        editable["instance"] = sorted(instance.id for instance in instances)
        editable["github_target"] = sorted(target.id for target in targets)
    else:
        for mapping in ctx.mappings:
            if mapping.role != "admin" or mapping.resource_id == models.SCOPE_ALL:
                continue
            if mapping.resource_type in editable:
                editable[mapping.resource_type].append(mapping.resource_id)
        editable = {key: sorted(set(value)) for key, value in editable.items()}
    return schemas.CurrentAccessOut(
        role=ctx.role,
        groups=ctx.groups,
        scoping_active=ctx.scoping_active,
        app_admin=ctx.app_admin,
        editable=editable,
    )


@router.get("/known-groups", response_model=list[str])
def known_groups(db: Session = Depends(get_db), _: AccessContext = Depends(require_app_admin())):
    seen = {g.name for g in db.query(models.SeenOidcGroup).all()}
    mapped = {
        oidc_group
        for (oidc_group,) in db.query(models.AccessMapping.oidc_group).distinct().all()
    }
    return sorted(seen | mapped)


def _resource_name(db: Session, resource_type: str, resource_id: str) -> str:
    if resource_type == models.SCOPE_ALL:
        return "(all resources)"
    if resource_type == "instance":
        obj = db.get(models.NetboxInstance, resource_id)
    else:
        obj = db.get(models.GithubTarget, resource_id)
    return obj.name if obj else "(deleted)"


def _mapping_out(db: Session, mapping: models.AccessMapping) -> schemas.AccessMappingOut:
    return schemas.AccessMappingOut(
        id=mapping.id,
        oidc_group=mapping.oidc_group,
        role=mapping.role,
        resource_type=mapping.resource_type,
        resource_id=mapping.resource_id,
        resource_name=_resource_name(db, mapping.resource_type, mapping.resource_id),
        created_at=mapping.created_at,
        updated_at=mapping.updated_at,
    )


def _validated_scope(db: Session, resource_type: str, resource_id: str) -> tuple[str, str]:
    if resource_type not in models.RESOURCE_TYPES:
        raise HTTPException(400, f"resource_type must be one of {models.RESOURCE_TYPES}")
    if resource_type == models.SCOPE_ALL:
        return models.SCOPE_ALL, models.SCOPE_ALL
    if resource_id == models.SCOPE_ALL:
        raise HTTPException(400, "resource_id must identify a concrete resource.")

    model = models.NetboxInstance if resource_type == "instance" else models.GithubTarget
    if db.get(model, resource_id) is None:
        raise HTTPException(404, "Resource not found.")
    return resource_type, resource_id


def _validate_role(role: str | None) -> None:
    if role is not None and role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES} or null")


def _duplicate_exists(
    db: Session,
    oidc_group: str,
    resource_type: str,
    resource_id: str,
    exclude_id: str | None = None,
) -> bool:
    query = db.query(models.AccessMapping).filter_by(
        oidc_group=oidc_group,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    if exclude_id is not None:
        query = query.filter(models.AccessMapping.id != exclude_id)
    return query.first() is not None


@router.get("/mappings", response_model=list[schemas.AccessMappingOut])
def list_mappings(db: Session = Depends(get_db), _: AccessContext = Depends(require_app_admin())):
    mappings = (
        db.query(models.AccessMapping)
        .order_by(
            models.AccessMapping.oidc_group,
            models.AccessMapping.resource_type,
            models.AccessMapping.resource_id,
        )
        .all()
    )
    return [_mapping_out(db, mapping) for mapping in mappings]


@router.post("/mappings", response_model=schemas.AccessMappingOut, status_code=201)
def create_mapping(
    payload: schemas.AccessMappingCreate,
    db: Session = Depends(get_db),
    _: AccessContext = Depends(require_app_admin()),
):
    _validate_role(payload.role)
    resource_type, resource_id = _validated_scope(
        db, payload.resource_type, payload.resource_id
    )
    if _duplicate_exists(db, payload.oidc_group, resource_type, resource_id):
        raise HTTPException(
            400,
            "This group already has a mapping for this scope — edit or delete it instead.",
        )
    mapping = models.AccessMapping(
        oidc_group=payload.oidc_group,
        role=payload.role,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return _mapping_out(db, mapping)


@router.patch("/mappings/{mapping_id}", response_model=schemas.AccessMappingOut)
def update_mapping(
    mapping_id: str,
    payload: schemas.AccessMappingCreate,
    db: Session = Depends(get_db),
    _: AccessContext = Depends(require_app_admin()),
):
    mapping = db.get(models.AccessMapping, mapping_id)
    if mapping is None:
        raise HTTPException(404, "Access mapping not found.")
    _validate_role(payload.role)
    resource_type, resource_id = _validated_scope(
        db, payload.resource_type, payload.resource_id
    )
    if _duplicate_exists(
        db, payload.oidc_group, resource_type, resource_id, exclude_id=mapping.id
    ):
        raise HTTPException(
            400,
            "This group already has a mapping for this scope — edit or delete it instead.",
        )
    mapping.oidc_group = payload.oidc_group
    mapping.role = payload.role
    mapping.resource_type = resource_type
    mapping.resource_id = resource_id
    db.commit()
    db.refresh(mapping)
    return _mapping_out(db, mapping)


@router.delete("/mappings/{mapping_id}", status_code=204)
def delete_mapping(
    mapping_id: str, db: Session = Depends(get_db), _: AccessContext = Depends(require_app_admin())
):
    mapping = db.get(models.AccessMapping, mapping_id)
    if mapping is None:
        raise HTTPException(404, "Access mapping not found.")
    db.delete(mapping)
    db.commit()
