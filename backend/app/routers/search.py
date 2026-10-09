from concurrent.futures import ThreadPoolExecutor, wait

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.database import get_db
from app.rbac import AccessContext, get_access_context, sql_scope_condition
from app.services import netbox_client
from app.config import settings

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("", response_model=schemas.SearchResponse)
def search(
    query: str = Query(..., min_length=1),
    instance_ids: list[str] | None = Query(None),
    limit: int = Query(settings.search_limit_per_type, ge=1, le=1000),
    timeout_seconds: float = Query(settings.search_timeout_seconds, ge=1, le=60),
    db: Session = Depends(get_db),
    ctx: AccessContext = Depends(get_access_context),
):
    instances_query = db.query(models.NetboxInstance)
    condition = sql_scope_condition(models.NetboxInstance.id, "instance", ctx)
    if condition is not None:
        instances_query = instances_query.filter(condition)
    if instance_ids:
        instances_query = instances_query.filter(models.NetboxInstance.id.in_(set(instance_ids)))
    instances = instances_query.order_by(models.NetboxInstance.name).all()
    if not instances:
        raise HTTPException(400, "No matching NetBox instances configured.")

    def run_one(instance: models.NetboxInstance):
        token = crypto.decrypt(instance.api_token_encrypted)
        try:
            data = netbox_client.search_instance(
                instance.base_url, token, netbox_client.verify_for_instance(instance), query, limit=limit
            )
            return schemas.InstanceSearchResult(
                instance_id=instance.id,
                instance_name=instance.name,
                devices=data["devices"][:limit], virtual_machines=data["virtual_machines"][:limit],
                virtual_device_contexts=data["virtual_device_contexts"][:limit],
                ip_addresses=data["ip_addresses"][:limit], prefixes=data["prefixes"][:limit],
                mac_addresses=data["mac_addresses"][:limit],
                truncated=data["truncated"],
            )
        except Exception as exc:
            return schemas.InstanceSearchResult(
                instance_id=instance.id, instance_name=instance.name, error=str(exc)
            )

    results = []
    executor = ThreadPoolExecutor(max_workers=8)
    try:
        futures = {executor.submit(run_one, inst): inst for inst in instances}
        done, pending = wait(futures, timeout=timeout_seconds)
        for future in done:
            results.append(future.result())
        for future in pending:
            future.cancel()
            instance = futures[future]
            results.append(schemas.InstanceSearchResult(
                instance_id=instance.id, instance_name=instance.name,
                error=f"Search timed out after {timeout_seconds:g} seconds",
            ))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    # Keep result order stable regardless of which instance responded first.
    order = {inst.id: idx for idx, inst in enumerate(instances)}
    results.sort(key=lambda r: order.get(r.instance_id, 0))

    return schemas.SearchResponse(query=query, results=results)
