import logging
import contextvars
import json
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse
from sqlalchemy import text

from app import models
from app.auth import get_current_user_optional
from app.config import settings, validate_startup_security
from app.database import SessionLocal, optimize_database, run_schema_migrations
from app.rbac import AccessContext, require_app_admin
from app import stats
from app.observability import (
    get_request_stats, install_db_instrumentation, reset_request_stats, start_request_stats,
)
from app.database import engine
from app.routers import auth, device_types, drift, exports, fleet, github, instances, migrations, module_types, rack_types, reference_data, search, tenant_permissions
from app.routers import audit as audit_router
from app.routers import custom_fields
from app.routers import syslog as syslog_router
from app.routers import access as access_router
from app.services import syslog_client  # noqa: F401  (import registers the audit -> syslog event listener)
from app.services import github_repo, netbox_client
from app.services.export import cleanup as export_cleanup, runner as export_runner, storage as export_storage
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

request_id_context = contextvars.ContextVar("request_id", default="-")


class RequestContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_context.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({"level": record.levelname.lower(), "logger": record.name,
                           "message": record.getMessage(), "request_id": getattr(record, "request_id", "-")})


def configure_logging() -> None:
    for handler in logging.getLogger().handlers:
        handler.addFilter(RequestContextFilter())
        if settings.log_format.lower() == "json":
            handler.setFormatter(JsonFormatter())


configure_logging()
install_db_instrumentation(engine)

validate_startup_security()

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


@asynccontextmanager
async def lifespan(application: FastAPI):
    github_repo.initialize_archive_limit()
    run_schema_migrations()
    export_storage.ensure_dirs()
    export_cleanup.recover_after_restart()
    start_scheduler()
    resume_orphaned_migration_jobs()
    try:
        yield
    finally:
        scheduler = getattr(application.state, "scheduler", None)
        if scheduler is not None:
            scheduler.shutdown(wait=False)
            application.state.scheduler = None
        netbox_client.close_sessions()
        export_runner.shutdown()
        optimize_database()

app = FastAPI(
    title="NetBox Manager API", version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.enable_api_docs else None,
    redoc_url="/redoc" if settings.enable_api_docs else None,
    openapi_url="/openapi.json" if settings.enable_api_docs else None,
)

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
            and (path.startswith("/api/") or path in {"/docs", "/redoc", "/openapi.json"})
            and not any(path.startswith(p) for p in _PUBLIC_API_PREFIXES)
        ):
            if get_current_user_optional(request) is None:
                return JSONResponse({"detail": "Not authenticated."}, status_code=401)
        return await call_next(request)


app.add_middleware(AuthRequiredMiddleware)


class MutationAuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
            parts = [part for part in request.url.path.split("/") if part]
            category = parts[1] if len(parts) > 1 else "api"
            resource_type = {
                "instances": "instance", "github-targets": "github_target",
                "migrations": "migration", "access-mappings": "access_mapping",
                "auth": "authentication", "drift": "drift",
                "exports": "export",
            }.get(category, category)
            resource_id = next((part for part in parts[2:] if part not in {
                "test", "plan", "execute", "cancel", "retry-failed", "rollback", "push", "check",
            }), None)
            user = get_current_user_optional(request) or {}
            db = SessionLocal()
            try:
                db.add(models.AuditEvent(
                    actor_sub=user.get("sub"), actor_name=user.get("name"), actor_email=user.get("email"),
                    action=f"{request.method.lower()}_{category}", resource_type=resource_type,
                    resource_id=resource_id, status="success" if response.status_code < 400 else "error",
                    detail=f"HTTP {response.status_code}",
                ))
                db.commit()
            except Exception:
                logger.exception("Could not persist audit event for %s %s", request.method, request.url.path)
                db.rollback()
            finally:
                db.close()
        return response


app.add_middleware(MutationAuditMiddleware)


_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class RequestMetricsMiddleware:
    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.request_timing:
            await self.application(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        request_id = supplied if _SAFE_REQUEST_ID.fullmatch(supplied) else str(uuid.uuid4())
        token = request_id_context.set(request_id)
        stats_token = start_request_stats()
        started = time.perf_counter()
        status = 500

        async def send_with_request_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode("ascii")))
            await send(message)

        try:
            await self.application(scope, receive, send_with_request_id)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            route = scope.get("route")
            route_name = getattr(route, "path", scope.get("path", ""))
            method = scope.get("method", "")
            stats.record_request(f"{method} {route_name}", elapsed_ms)
            calls = get_request_stats()
            payload = {
                "event": "request_timing", "request_id": request_id, "method": method,
                "route": route_name, "status": status, "total_ms": round(elapsed_ms, 2),
                "outbound": {name: {"count": metric.count, "seconds": round(metric.seconds, 6)}
                             for name, metric in calls.outbound.items()},
                "outbound_seconds": round(sum(metric.seconds for metric in calls.outbound.values()), 6),
                "db_count": calls.db_count, "db_ms": round(calls.db_seconds * 1000, 2),
                "slowest_db_ms": round(calls.slowest_db_seconds * 1000, 2),
                "slowest_db_statement": calls.slowest_db_statement,
            }
            logger.log(logging.WARNING if elapsed_ms > settings.slow_request_ms else logging.INFO,
                       json.dumps(payload, separators=(",", ":")))
            reset_request_stats(stats_token)
            request_id_context.reset(token)


app.add_middleware(RequestMetricsMiddleware)

app.include_router(auth.router)
app.include_router(instances.router)
app.include_router(github.router)
app.include_router(device_types.router)
app.include_router(module_types.router)
app.include_router(rack_types.router)
app.include_router(search.router)
app.include_router(drift.router)
app.include_router(audit_router.router)
app.include_router(fleet.router)
app.include_router(custom_fields.router)
app.include_router(reference_data.router)
app.include_router(syslog_router.router)
app.include_router(access_router.router)
app.include_router(tenant_permissions.router)
app.include_router(migrations.router)
app.include_router(exports.router)


def _run_scheduled_drift_check():
    from app.services import drift as drift_mod
    db = SessionLocal()
    try:
        drift_mod.run_full_check(db)
    finally:
        db.close()


def _purge_old_audit_records():
    if settings.audit_retention_days <= 0:
        return
    from datetime import timedelta
    cutoff = utcnow() - timedelta(days=settings.audit_retention_days)
    db = SessionLocal()
    try:
        db.query(models.AuditEvent).filter(models.AuditEvent.created_at < cutoff).delete(synchronize_session=False)
        db.query(models.DeviceTypePushHistory).filter(
            models.DeviceTypePushHistory.created_at < cutoff
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def start_scheduler():
    configure_logging()
    from app.auth import purge_expired_sessions
    purge_expired_sessions()
    from apscheduler.schedulers.background import BackgroundScheduler

    # Pin the scheduler to UTC so the minimal runtime does not need an OS
    # timezone database just to discover the container's local timezone.
    scheduler = BackgroundScheduler(timezone="UTC")
    if settings.drift_check_interval_hours > 0:
        scheduler.add_job(
            _run_scheduled_drift_check, "interval", hours=settings.drift_check_interval_hours,
            id="drift_check", replace_existing=True,
        )
    scheduler.add_job(_purge_old_audit_records, "interval", days=1, id="audit_retention", replace_existing=True)
    scheduler.add_job(purge_expired_sessions, "interval", hours=1, id="session_retention", replace_existing=True)
    scheduler.add_job(export_cleanup.run_cleanup, "interval", minutes=15, id="export_cleanup", replace_existing=True)
    export_cleanup.run_cleanup()
    scheduler.start()
    app.state.scheduler = scheduler


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
    from app import models
    from app.routers.migrations import (
        _run_in_background, _run_plan_in_background, _run_rollback_in_background, start_job_worker,
    )

    db = SessionLocal()
    try:
        jobs = db.query(models.MigrationJob).filter(models.MigrationJob.status.in_(
            {"running", "cancelling", "planning", "rolling_back"}
        )).all()
        for job in jobs:
            if not settings.migration_auto_resume or job.resume_count >= settings.migration_max_auto_resumes:
                reason = ("Automatic migration resume is disabled. Retry the job from the UI."
                          if not settings.migration_auto_resume else
                          f"Automatic resume limit ({settings.migration_max_auto_resumes}) reached. Retry from the UI.")
                job.status = "failed"
                job.current_step = reason
                continue
            job.resume_count += 1
            job.last_resumed_at = utcnow()
            db.commit()
            if job.status in {"running", "cancelling"}:
                options = _json.loads(job.options_json or "{}")
                worker, args = _run_in_background, (options.get("max_requests_per_second", 4.0),)
            elif job.status == "planning":
                worker, args = _run_plan_in_background, ()
            else:
                worker, args = _run_rollback_in_background, ()
            logger.warning("Automatically resuming orphaned migration job %s (%s).", job.id, job.status)
            syslog_client.send_audit_entry({
                "action_type": "migration_auto_resume", "target_type": "migration",
                "target_name": job.id, "status": "success", "detail": f"Resumed {job.status}",
            })
            start_job_worker(job.id, worker, args)
        db.commit()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/health/ready")
def readiness(response: Response):
    db = SessionLocal()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        db.rollback()
        if settings.drift_check_interval_hours > 0 and not getattr(app.state, "scheduler", None):
            raise RuntimeError("scheduler unavailable")
        return {"status": "ok"}
    except Exception:
        response.status_code = 503
        return {"status": "fail"}
    finally:
        db.close()


@app.get("/api/admin/stats")
def admin_stats(_: AccessContext = Depends(require_app_admin())):
    return stats.snapshot()


@app.get("/")
def root():
    return {"service": "netbox-manager-api", "docs": "/docs"}
