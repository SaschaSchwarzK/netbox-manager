from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.database import get_db
from app.rbac import AccessContext, filter_scoped, get_access_context, require_role
from app.services import netbox_client

router = APIRouter(prefix="/api/instances", tags=["instances"])


@router.get("", response_model=list[schemas.NetboxInstanceOut])
def list_instances(db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    instances = db.query(models.NetboxInstance).order_by(models.NetboxInstance.name).all()
    return filter_scoped(instances, "instance", ctx, db)


@router.post("", response_model=schemas.NetboxInstanceOut, status_code=201)
def create_instance(
    payload: schemas.NetboxInstanceCreate, db: Session = Depends(get_db),
    _: AccessContext = Depends(require_role("admin")),
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
    _: AccessContext = Depends(require_role("admin")),
):
    instance = db.get(models.NetboxInstance, instance_id)
    if not instance:
        raise HTTPException(404, "Instance not found.")
    if payload.name is not None:
        instance.name = payload.name
    if payload.base_url is not None:
        instance.base_url = payload.base_url.rstrip("/")
    if payload.api_token is not None:
        instance.api_token_encrypted = crypto.encrypt(payload.api_token)
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
    instance_id: str, db: Session = Depends(get_db), _: AccessContext = Depends(require_role("admin"))
):
    instance = db.get(models.NetboxInstance, instance_id)
    if not instance:
        raise HTTPException(404, "Instance not found.")
    db.delete(instance)
    db.commit()


@router.post("/{instance_id}/test", response_model=schemas.ConnectionTestResult)
def test_instance(instance_id: str, db: Session = Depends(get_db)):
    instance = db.get(models.NetboxInstance, instance_id)
    if not instance:
        raise HTTPException(404, "Instance not found.")
    token = crypto.decrypt(instance.api_token_encrypted)
    result = netbox_client.test_connection(instance.base_url, token, instance.verify_ssl)
    return result


@router.post("/test", response_model=schemas.ConnectionTestResult)
def test_new_instance(payload: schemas.NetboxInstanceCreate):
    """Test a connection before saving it (used by the 'Add instance' form)."""
    result = netbox_client.test_connection(
        payload.base_url.rstrip("/"), payload.api_token, payload.verify_ssl
    )
    return result
