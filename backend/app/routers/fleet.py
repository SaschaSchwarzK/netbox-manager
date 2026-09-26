from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.database import get_db
from app.rbac import AccessContext, filter_scoped, get_access_context
from app.services import github_repo, netbox_client

router = APIRouter(prefix="/api/fleet", tags=["fleet"])


@router.get("/health", response_model=list[schemas.InstanceHealthOut])
def fleet_health(db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    instances = db.query(models.NetboxInstance).all()
    instances = filter_scoped(instances, "instance", ctx, db)

    def check_one(instance: models.NetboxInstance) -> schemas.InstanceHealthOut:
        token = crypto.decrypt(instance.api_token_encrypted)
        health = netbox_client.get_health(instance.base_url, token, instance.verify_ssl)
        expiry = netbox_client.check_token_expiry(instance.base_url, token, instance.verify_ssl)
        return schemas.InstanceHealthOut(
            instance_id=instance.id, instance_name=instance.name,
            reachable=health["reachable"], netbox_version=health["netbox_version"],
            python_version=health["python_version"], plugins=health["plugins"],
            response_time_ms=health["response_time_ms"], error=health["error"],
            token_expiry=schemas.TokenExpiryInfo(**expiry),
        )

    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(check_one, inst) for inst in instances]
        for future in as_completed(futures):
            results.append(future.result())

    order = {inst.id: idx for idx, inst in enumerate(instances)}
    results.sort(key=lambda r: order.get(r.instance_id, 0))
    return results


@router.get("/github-token-status", response_model=list[schemas.GithubTokenStatusOut])
def github_token_status(db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    targets = db.query(models.GithubTarget).all()
    targets = filter_scoped(targets, "github_target", ctx, db)

    def check_one(target: models.GithubTarget) -> schemas.GithubTokenStatusOut:
        pat = crypto.decrypt(target.pat_encrypted)
        expiry = github_repo.check_token_expiry(pat)
        return schemas.GithubTokenStatusOut(
            target_id=target.id, target_name=target.name, token_expiry=schemas.TokenExpiryInfo(**expiry)
        )

    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(check_one, t) for t in targets]
        for future in as_completed(futures):
            results.append(future.result())

    order = {t.id: idx for idx, t in enumerate(targets)}
    results.sort(key=lambda r: order.get(r.target_id, 0))
    return results
