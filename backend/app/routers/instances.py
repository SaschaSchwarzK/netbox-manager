from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.database import get_db
from app.rbac import (
    AccessContext,
    filter_scoped,
    get_access_context,
    require_app_admin,
    require_resource_admin,
    require_visible,
)
from app.services import netbox_client

router = APIRouter(prefix="/api/instances", tags=["instances"])


@router.get("", response_model=list[schemas.NetboxInstanceOut])
def list_instances(db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    instances = db.query(models.NetboxInstance).order_by(models.NetboxInstance.name).all()
    return filter_scoped(instances, "instance", ctx, db)


@router.post("", response_model=schemas.NetboxInstanceOut, status_code=201)
def create_instance(
    payload: schemas.NetboxInstanceCreate, db: Session = Depends(get_db),
    _: AccessContext = Depends(require_app_admin()),
):
    if db.query(models.NetboxInstance).filter_by(name=payload.name).first():
        raise HTTPException(400, "An instance with that name already exists.")
    instance = models.NetboxInstance(
        name=payload.name,
        base_url=payload.base_url.rstrip("/"),
        api_token_encrypted=crypto.encrypt(payload.api_token),
        verify_ssl=payload.verify_ssl,
        description=payload.description,
        requires_approved_pr=payload.requires_approved_pr,
    )
    instance.tags = payload.tags
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


@router.patch("/{instance_id}", response_model=schemas.NetboxInstanceOut)
def update_instance(
    instance_id: str, payload: schemas.NetboxInstanceUpdate, db: Session = Depends(get_db),
    ctx: AccessContext = Depends(get_access_context),
):
    instance = db.get(models.NetboxInstance, instance_id)
    if not instance:
        raise HTTPException(404, "Instance not found.")
    require_resource_admin(ctx, "instance", instance_id, db)
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(400, "Instance name must not be empty.")
        duplicate = (
            db.query(models.NetboxInstance)
            .filter(models.NetboxInstance.name == name, models.NetboxInstance.id != instance_id)
            .first()
        )
        if duplicate:
            raise HTTPException(400, "An instance with that name already exists.")
        instance.name = name
    if payload.base_url is not None:
        base_url = payload.base_url.strip().rstrip("/")
        if not base_url:
            raise HTTPException(400, "Instance base URL must not be empty.")
        instance.base_url = base_url
    if payload.api_token is not None and payload.api_token.strip():
        instance.api_token_encrypted = crypto.encrypt(payload.api_token.strip())
    if payload.verify_ssl is not None:
        instance.verify_ssl = payload.verify_ssl
    if payload.description is not None:
        instance.description = payload.description
    if payload.tags is not None:
        instance.tags = payload.tags
    if payload.requires_approved_pr is not None:
        instance.requires_approved_pr = payload.requires_approved_pr
    db.commit()
    db.refresh(instance)
    return instance


@router.delete("/{instance_id}", status_code=204)
def delete_instance(
    instance_id: str, db: Session = Depends(get_db), _: AccessContext = Depends(require_app_admin())
):
    instance = db.get(models.NetboxInstance, instance_id)
    if not instance:
        raise HTTPException(404, "Instance not found.")
    db.query(models.AccessMapping).filter_by(
        resource_type="instance", resource_id=instance_id
    ).delete(synchronize_session=False)
    db.query(models.ScopeMapping).filter_by(
        resource_type="instance", resource_id=instance_id
    ).delete(synchronize_session=False)
    db.delete(instance)
    db.commit()


@router.post("/{instance_id}/test", response_model=schemas.ConnectionTestResult)
def test_instance(
    instance_id: str,
    db: Session = Depends(get_db),
    ctx: AccessContext = Depends(get_access_context),
):
    instance = db.get(models.NetboxInstance, instance_id)
    if not instance:
        raise HTTPException(404, "Instance not found.")
    require_visible("instance", instance_id, ctx, db)
    token = crypto.decrypt(instance.api_token_encrypted)
    result = netbox_client.test_connection(instance.base_url, token, instance.verify_ssl)
    return result


@router.post("/test", response_model=schemas.ConnectionTestResult)
def test_new_instance(
    payload: schemas.InstanceTestRequest,
    db: Session = Depends(get_db),
    ctx: AccessContext = Depends(get_access_context),
):
    """Test a connection before saving it (used by the 'Add instance' form)."""
    instance = None
    if payload.id is not None:
        instance = db.get(models.NetboxInstance, payload.id)
        if instance is None:
            raise HTTPException(404, "Instance not found.")
        require_resource_admin(ctx, "instance", payload.id, db)
    elif not ctx.app_admin:
        raise HTTPException(
            403,
            "Managing NetBox Manager itself requires app-level admin — a mapping with role "
            "'admin' on 'All resources', or NBM_BOOTSTRAP_ADMIN_GROUPS.",
        )

    base_url = payload.base_url.strip().rstrip("/") if payload.base_url else ""
    if not base_url and instance is not None:
        base_url = instance.base_url
    if not base_url:
        raise HTTPException(400, "Instance base URL is required.")

    if payload.api_token is not None and payload.api_token.strip():
        token = payload.api_token.strip()
    elif instance is not None:
        token = crypto.decrypt(instance.api_token_encrypted)
    else:
        raise HTTPException(400, "API token is required when testing a new instance.")

    verify_ssl = payload.verify_ssl
    if verify_ssl is None:
        verify_ssl = instance.verify_ssl if instance is not None else True
    result = netbox_client.test_connection(base_url, token, verify_ssl)
    return result
