import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

from app.auth import get_current_user_optional
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.routers import auth, device_types, drift, fleet, github, instances, migrations, search, tenant_permissions
from app.routers import audit as audit_router
from app.routers import custom_fields
from app.routers import syslog as syslog_router
from app.routers import access as access_router
from app.services import syslog_client  # noqa: F401  (import registers the audit -> syslog event listener)

logger = logging.getLogger(__name__)

_local_user_set = bool(settings.local_admin_user.strip())
_local_password_set = bool(settings.local_admin_password.strip())
_oidc_values = (
    settings.oidc_issuer.strip(),
    settings.oidc_client_id.strip(),
    settings.oidc_client_secret.strip(),
)
if settings.authentication_disabled:
    logger.warning(
        "Authentication and authorization are disabled by AUTHENTICATION_DISABLED=True."
    )
elif any(_oidc_values) and not settings.oidc_enabled:
    raise RuntimeError(
        "OIDC configuration is incomplete. Set NBM_OIDC_ISSUER, NBM_OIDC_CLIENT_ID, "
        "and NBM_OIDC_CLIENT_SECRET, or remove all three values."
    )
elif _local_user_set != _local_password_set:
    raise RuntimeError(
        "Local admin configuration is incomplete. Set both NBM_LOCAL_ADMIN_USER and "
        "NBM_LOCAL_ADMIN_PASSWORD, or remove both values."
    )
elif not settings.oidc_enabled and not settings.local_admin_enabled:
    raise RuntimeError(
        "Authentication is enforced, but no complete authentication method is configured. "
        "Configure OIDC, configure the local administrator, or explicitly set "
        "AUTHENTICATION_DISABLED=True."
    )

if not settings.authentication_disabled and settings.local_admin_enabled:
    logger.info("Break-glass local admin login is enabled.")

Base.metadata.create_all(bind=engine)

app = FastAPI(title="NetBox Manager API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Needed by authlib to stash OIDC state/nonce between the redirect to the
# provider and the callback; independent of our own session cookie above.
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, same_site="lax")

# Routes that must stay reachable without a session: the login/callback/logout/me
# flow itself, and the plain health check.
_PUBLIC_API_PREFIXES = ("/api/auth", "/api/health")


class AuthRequiredMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if (
            not settings.authentication_disabled
            and request.method != "OPTIONS"  # never block CORS preflight; it carries no cookies anyway
            and path.startswith("/api/")
            and not any(path.startswith(p) for p in _PUBLIC_API_PREFIXES)
        ):
            if get_current_user_optional(request) is None:
                return JSONResponse({"detail": "Not authenticated."}, status_code=401)
        return await call_next(request)


app.add_middleware(AuthRequiredMiddleware)

app.include_router(auth.router)
app.include_router(instances.router)
app.include_router(github.router)
app.include_router(device_types.router)
app.include_router(search.router)
app.include_router(drift.router)
app.include_router(audit_router.router)
app.include_router(fleet.router)
app.include_router(custom_fields.router)
app.include_router(syslog_router.router)
app.include_router(access_router.router)
app.include_router(tenant_permissions.router)
app.include_router(migrations.router)


def _run_scheduled_drift_check():
    from app.services import drift as drift_mod
    db = SessionLocal()
    try:
        drift_mod.run_full_check(db)
    finally:
        db.close()


@app.on_event("startup")
def start_scheduler():
    if settings.drift_check_interval_hours <= 0:
        return  # set NBM_DRIFT_CHECK_INTERVAL_HOURS=0 to disable periodic checks entirely
    from apscheduler.schedulers.background import BackgroundScheduler

    # Pin the scheduler to UTC so the minimal runtime does not need an OS
    # timezone database just to discover the container's local timezone.
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _run_scheduled_drift_check,
        "interval",
        hours=settings.drift_check_interval_hours,
        id="drift_check",
        replace_existing=True,
    )
    scheduler.start()
    app.state.scheduler = scheduler


@app.on_event("startup")
def resume_orphaned_migration_jobs():
    """
    Any MigrationJob found with status="running" at startup is unambiguously
    orphaned: this is a single-process deployment (see README — one
    container, no task queue), so the process that owned it is simply gone
    (crashed, was killed, or the container restarted). It is always safe to
    resume such a job without any lease or heartbeat comparison — there is
    no other process it could still belong to. See
    services/migration/executor.py for why resuming is safe (every write is
    committed immediately, and creates are re-checked by natural key first).
    """
    import json as _json
    import threading

    from app import models
    from app.routers.migrations import _run_in_background, _run_rollback_in_background

    db = SessionLocal()
    try:
        orphaned = db.query(models.MigrationJob).filter(models.MigrationJob.status == "running").all()
        for job in orphaned:
            options = _json.loads(job.options_json or "{}")
            max_rps = options.get("max_requests_per_second", 4.0)
            logger.warning("Resuming orphaned migration job %s (found status=running at startup).", job.id)
            threading.Thread(target=_run_in_background, args=(job.id, max_rps), daemon=True).start()
        orphaned_rollbacks = db.query(models.MigrationJob).filter(models.MigrationJob.status == "rolling_back").all()
        for job in orphaned_rollbacks:
            logger.warning("Resuming orphaned migration rollback %s at startup.", job.id)
            threading.Thread(target=_run_rollback_in_background, args=(job.id,), daemon=True).start()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"service": "netbox-manager-api", "docs": "/docs"}
