from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

from app.auth import get_current_user_optional
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.migrations import run_lightweight_migrations
from app.routers import auth, device_types, drift, fleet, github, instances, search
from app.routers import audit as audit_router
from app.routers import custom_fields
from app.routers import syslog as syslog_router
from app.routers import access as access_router
from app.services import syslog_client  # noqa: F401  (import registers the audit -> syslog event listener)

Base.metadata.create_all(bind=engine)
run_lightweight_migrations(engine, Base)

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
            settings.oidc_issuer  # auth is configured at all
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

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _run_scheduled_drift_check,
        "interval",
        hours=settings.drift_check_interval_hours,
        id="drift_check",
        replace_existing=True,
    )
    scheduler.start()
    app.state.scheduler = scheduler


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"service": "netbox-manager-api", "docs": "/docs"}
