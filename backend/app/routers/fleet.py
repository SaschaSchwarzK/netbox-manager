import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.database import get_db
from app.rbac import AccessContext, get_access_context
from app.rbac import sql_scope_condition
from app.config import settings
from app.services import github_repo, netbox_client

router = APIRouter(prefix="/api/fleet", tags=["fleet"])
_cache: dict[tuple[str, str], tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _cached(kind: str, id_: str):
    with _cache_lock:
        cached = _cache.get((kind, id_))
        if cached and cached[0] > time.monotonic():
            return cached[1]
        if cached:
            _cache.pop((kind, id_), None)
    return None


def _store(kind: str, id_: str, value):
    if settings.fleet_cache_seconds > 0:
        with _cache_lock:
            _cache[(kind, id_)] = (time.monotonic() + settings.fleet_cache_seconds, value)


@router.get("/health", response_model=list[schemas.InstanceHealthOut])
def fleet_health(limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
                 refresh: bool = Query(False),
                 db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    query = db.query(models.NetboxInstance)
    condition = sql_scope_condition(models.NetboxInstance.id, "instance", ctx)
    if condition is not None:
        query = query.filter(condition)
    instances = query.order_by(models.NetboxInstance.name).offset(offset).limit(limit).all()

    def check_one(instance: models.NetboxInstance) -> schemas.InstanceHealthOut:
        token = crypto.decrypt(instance.api_token_encrypted)
        health = netbox_client.get_health(instance.base_url, token, netbox_client.verify_for_instance(instance))
        expiry = netbox_client.check_token_expiry(instance.base_url, token, netbox_client.verify_for_instance(instance))
        result = schemas.InstanceHealthOut(
            instance_id=instance.id, instance_name=instance.name,
            reachable=health["reachable"], netbox_version=health["netbox_version"],
            python_version=health["python_version"], plugins=health["plugins"],
            response_time_ms=health["response_time_ms"], error=health["error"],
            token_expiry=schemas.TokenExpiryInfo(**expiry),
        )
        _store("netbox", instance.id, result)
        return result

    results = []
    pending_instances = []
    for instance in instances:
        cached = None if refresh else _cached("netbox", instance.id)
        if cached is not None:
            results.append(cached)
        else:
            pending_instances.append(instance)
    executor = ThreadPoolExecutor(max_workers=8)
    try:
        futures = {executor.submit(check_one, instance): instance for instance in pending_instances}
        done, pending = wait(futures, timeout=settings.fleet_timeout_seconds)
        results.extend(future.result() for future in done)
        for future in pending:
            future.cancel()
            instance = futures[future]
            results.append(schemas.InstanceHealthOut(
                instance_id=instance.id, instance_name=instance.name, reachable=False,
                response_time_ms=int(settings.fleet_timeout_seconds * 1000),
                error=f"Fleet check timed out after {settings.fleet_timeout_seconds:g} seconds",
                token_expiry=schemas.TokenExpiryInfo(known=False, expires=None, note="Check timed out"),
            ))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    order = {inst.id: idx for idx, inst in enumerate(instances)}
    results.sort(key=lambda r: order.get(r.instance_id, 0))
    return results


@router.get("/github-token-status", response_model=list[schemas.GithubTokenStatusOut])
def github_token_status(limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
                        refresh: bool = Query(False),
                        db: Session = Depends(get_db), ctx: AccessContext = Depends(get_access_context)):
    query = db.query(models.GithubTarget)
    condition = sql_scope_condition(models.GithubTarget.id, "github_target", ctx)
    if condition is not None:
        query = query.filter(condition)
    targets = query.order_by(models.GithubTarget.name).offset(offset).limit(limit).all()

    def check_one(target: models.GithubTarget) -> schemas.GithubTokenStatusOut:
        pat = crypto.decrypt(target.pat_encrypted)
        expiry = github_repo.check_token_expiry(pat)
        result = schemas.GithubTokenStatusOut(
            target_id=target.id, target_name=target.name, token_expiry=schemas.TokenExpiryInfo(**expiry)
        )
        _store("github", target.id, result)
        return result

    results = []
    pending_targets = []
    for target in targets:
        cached = None if refresh else _cached("github", target.id)
        if cached is not None:
            results.append(cached)
        else:
            pending_targets.append(target)
    executor = ThreadPoolExecutor(max_workers=8)
    try:
        futures = {executor.submit(check_one, target): target for target in pending_targets}
        done, pending = wait(futures, timeout=settings.fleet_timeout_seconds)
        results.extend(future.result() for future in done)
        for future in pending:
            future.cancel()
            target = futures[future]
            results.append(schemas.GithubTokenStatusOut(
                target_id=target.id, target_name=target.name,
                token_expiry=schemas.TokenExpiryInfo(known=False, expires=None, note="Check timed out"),
            ))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    order = {t.id: idx for idx, t in enumerate(targets)}
    results.sort(key=lambda r: order.get(r.target_id, 0))
    return results
