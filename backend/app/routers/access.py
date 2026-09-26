from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db
from app.rbac import AccessContext, ROLES, require_role

router = APIRouter(prefix="/api/access", tags=["access"])


@router.get("/me", response_model=schemas.CurrentAccessOut)
def current_access(ctx: AccessContext = Depends(require_role("viewer"))):
    return schemas.CurrentAccessOut(role=ctx.role, groups=ctx.groups, scoping_active=ctx.scoping_active)


@router.get("/known-groups", response_model=list[str])
def known_groups(db: Session = Depends(get_db), _: AccessContext = Depends(require_role("admin"))):
    seen = {g.name for g in db.query(models.SeenOidcGroup).all()}
    mapped = {m.oidc_group for m in db.query(models.RoleMapping).all()}
    scoped = {m.oidc_group for m in db.query(models.ScopeMapping).all()}
    return sorted(seen | mapped | scoped)


# ---------- Role mappings ----------

@router.get("/role-mappings", response_model=list[schemas.RoleMappingOut])
def list_role_mappings(db: Session = Depends(get_db), _: AccessContext = Depends(require_role("admin"))):
    return db.query(models.RoleMapping).order_by(models.RoleMapping.oidc_group).all()


@router.post("/role-mappings", response_model=schemas.RoleMappingOut, status_code=201)
def create_role_mapping(
    payload: schemas.RoleMappingCreate, db: Session = Depends(get_db), _: AccessContext = Depends(require_role("admin"))
):
    if payload.role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")
    if db.query(models.RoleMapping).filter_by(oidc_group=payload.oidc_group).first():
        raise HTTPException(400, "This group already has a role mapping — edit or delete the existing one instead.")
    mapping = models.RoleMapping(oidc_group=payload.oidc_group, role=payload.role)
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return mapping


@router.patch("/role-mappings/{mapping_id}", response_model=schemas.RoleMappingOut)
def update_role_mapping(
    mapping_id: str, payload: schemas.RoleMappingCreate, db: Session = Depends(get_db),
    _: AccessContext = Depends(require_role("admin")),
):
    mapping = db.get(models.RoleMapping, mapping_id)
    if not mapping:
        raise HTTPException(404, "Role mapping not found.")
    if payload.role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")
    mapping.oidc_group = payload.oidc_group
    mapping.role = payload.role
    db.commit()
    db.refresh(mapping)
    return mapping


@router.delete("/role-mappings/{mapping_id}", status_code=204)
def delete_role_mapping(
    mapping_id: str, db: Session = Depends(get_db), _: AccessContext = Depends(require_role("admin"))
):
    mapping = db.get(models.RoleMapping, mapping_id)
    if not mapping:
        raise HTTPException(404, "Role mapping not found.")
    db.delete(mapping)
    db.commit()


# ---------- Scope mappings ----------

def _resource_name(db: Session, resource_type: str, resource_id: str) -> str:
    if resource_type == "instance":
        obj = db.get(models.NetboxInstance, resource_id)
    else:
        obj = db.get(models.GithubTarget, resource_id)
    return obj.name if obj else "(deleted)"


@router.get("/scope-mappings", response_model=list[schemas.ScopeMappingOut])
def list_scope_mappings(db: Session = Depends(get_db), _: AccessContext = Depends(require_role("admin"))):
    mappings = db.query(models.ScopeMapping).order_by(models.ScopeMapping.oidc_group).all()
    return [
        schemas.ScopeMappingOut(
            id=m.id, oidc_group=m.oidc_group, resource_type=m.resource_type, resource_id=m.resource_id,
            resource_name=_resource_name(db, m.resource_type, m.resource_id), created_at=m.created_at,
        )
        for m in mappings
    ]


@router.post("/scope-mappings", response_model=schemas.ScopeMappingOut, status_code=201)
def create_scope_mapping(
    payload: schemas.ScopeMappingCreate, db: Session = Depends(get_db), _: AccessContext = Depends(require_role("admin"))
):
    if payload.resource_type not in ("instance", "github_target"):
        raise HTTPException(400, "resource_type must be 'instance' or 'github_target'.")
    exists = db.query(models.ScopeMapping).filter_by(
        oidc_group=payload.oidc_group, resource_type=payload.resource_type, resource_id=payload.resource_id
    ).first()
    if exists:
        raise HTTPException(400, "This group already has a scope mapping for this resource.")
    mapping = models.ScopeMapping(oidc_group=payload.oidc_group, resource_type=payload.resource_type, resource_id=payload.resource_id)
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return schemas.ScopeMappingOut(
        id=mapping.id, oidc_group=mapping.oidc_group, resource_type=mapping.resource_type, resource_id=mapping.resource_id,
        resource_name=_resource_name(db, mapping.resource_type, mapping.resource_id), created_at=mapping.created_at,
    )


@router.delete("/scope-mappings/{mapping_id}", status_code=204)
def delete_scope_mapping(
    mapping_id: str, db: Session = Depends(get_db), _: AccessContext = Depends(require_role("admin"))
):
    mapping = db.get(models.ScopeMapping, mapping_id)
    if not mapping:
        raise HTTPException(404, "Scope mapping not found.")
    db.delete(mapping)
    db.commit()
