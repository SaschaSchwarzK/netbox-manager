from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.database import get_db
from app.services import netbox_client

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("", response_model=schemas.SearchResponse)
def search(
    query: str = Query(..., min_length=1),
    instance_ids: list[str] | None = Query(None),
    db: Session = Depends(get_db),
):
    instances = db.query(models.NetboxInstance).all()
    if instance_ids:
        wanted = set(instance_ids)
        instances = [i for i in instances if i.id in wanted]
    if not instances:
        raise HTTPException(400, "No matching NetBox instances configured.")

    def run_one(instance: models.NetboxInstance):
        token = crypto.decrypt(instance.api_token_encrypted)
        try:
            data = netbox_client.search_instance(instance.base_url, token, instance.verify_ssl, query)
            return schemas.InstanceSearchResult(
                instance_id=instance.id,
                instance_name=instance.name,
                devices=data["devices"],
                virtual_machines=data["virtual_machines"],
                virtual_device_contexts=data["virtual_device_contexts"],
                ip_addresses=data["ip_addresses"],
                prefixes=data["prefixes"],
                mac_addresses=data["mac_addresses"],
            )
        except Exception as exc:
            return schemas.InstanceSearchResult(
                instance_id=instance.id, instance_name=instance.name, error=str(exc)
            )

    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(run_one, inst) for inst in instances]
        for future in as_completed(futures):
            results.append(future.result())

    # Keep result order stable regardless of which instance responded first.
    order = {inst.id: idx for idx, inst in enumerate(instances)}
    results.sort(key=lambda r: order.get(r.instance_id, 0))

    return schemas.SearchResponse(query=query, results=results)
