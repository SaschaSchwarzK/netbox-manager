"""
Session handling and the OIDC client registry.

We don't keep server-side session state: the session cookie itself carries
the user's identity (sub/email/name/groups) signed with itsdangerous, so any
backend replica can validate it without a shared session store. The cookie
is httponly and (in production) secure+samesite=lax, so it can't be read or
sent cross-site by JS, only presented back to us by the browser.
"""
import hashlib
import hmac
import json
import secrets
import threading
import time
from datetime import timedelta

from app import models
from app.config import settings
from app.database import SessionLocal
from app.timeutil import utcnow

SESSION_COOKIE = "nbm_session"
SESSION_MAX_AGE = settings.session_max_age_hours * 60 * 60
MAX_OIDC_GROUPS = 500

_local_login_failures: dict[tuple[str, str], list[float]] = {}
_local_login_failures_lock = threading.Lock()
_LOCAL_LOGIN_MAX_FAILURES = 10
_LOCAL_LOGIN_WINDOW_SECONDS = 5 * 60
_LOCAL_LOGIN_MAX_BUCKETS = 10_000


def _session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def normalize_groups(value) -> list[str]:
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return [item[:256] for item in values if isinstance(item, str) and item][:MAX_OIDC_GROUPS]


def create_session_cookie(user: dict, db=None) -> str:
    owns_db = db is None
    db = db or SessionLocal()
    session_id = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(models.UserSession(
        id_hash=_session_hash(session_id), sub=str(user.get("sub") or ""), name=user.get("name"),
        email=user.get("email"), username=user.get("username"),
        groups_json=json.dumps(normalize_groups(user.get("groups", []))), local=bool(user.get("local")),
        created_at=now, expires_at=now + timedelta(seconds=SESSION_MAX_AGE), last_seen_at=now,
    ))
    db.commit()
    if owns_db:
        db.close()
    return session_id


def create_local_session_cookie(username: str, db=None) -> str:
    return create_session_cookie(
        {
            "sub": f"local:{username}",
            "email": None,
            "name": f"local admin ({username})",
            "groups": [],
            "local": True,
        }, db
    )


def check_local_credentials(username: str, password: str) -> bool:
    username_matches = hmac.compare_digest(username, settings.local_admin_user)
    password_matches = hmac.compare_digest(password, settings.local_admin_password)
    return settings.local_admin_enabled and username_matches and password_matches


def _prune_local_failures(now: float) -> None:
    cutoff = now - _LOCAL_LOGIN_WINDOW_SECONDS
    expired = [key for key, failures in _local_login_failures.items() if not failures or failures[-1] <= cutoff]
    for key in expired:
        _local_login_failures.pop(key, None)
    while len(_local_login_failures) >= _LOCAL_LOGIN_MAX_BUCKETS:
        oldest = min(_local_login_failures, key=lambda key: _local_login_failures[key][-1])
        _local_login_failures.pop(oldest, None)


def _recent_local_failures(client_host: str, username: str, now: float) -> list[float]:
    key = (client_host, username)
    cutoff = now - _LOCAL_LOGIN_WINDOW_SECONDS
    failures = [stamp for stamp in _local_login_failures.get(key, []) if stamp > cutoff]
    if failures:
        _local_login_failures[key] = failures
    else:
        _local_login_failures.pop(key, None)
    return failures


def local_login_retry_after(client_host: str, username: str) -> int | None:
    with _local_login_failures_lock:
        now = time.monotonic()
        _prune_local_failures(now)
        failures = _recent_local_failures(client_host, username, now)
        if len(failures) < _LOCAL_LOGIN_MAX_FAILURES:
            return None
        return max(1, int(_LOCAL_LOGIN_WINDOW_SECONDS - (now - failures[0])))


def record_local_login_failure(client_host: str, username: str) -> None:
    with _local_login_failures_lock:
        now = time.monotonic()
        _prune_local_failures(now)
        failures = _recent_local_failures(client_host, username, now)
        failures.append(now)
        _local_login_failures[(client_host, username)] = failures


def clear_local_login_failures(client_host: str, username: str) -> None:
    with _local_login_failures_lock:
        _local_login_failures.pop((client_host, username), None)


def read_session_cookie(token: str | None) -> dict | None:
    if not token:
        return None
    db = SessionLocal()
    try:
        row = db.get(models.UserSession, _session_hash(token))
        now = utcnow()
        idle_cutoff = now - timedelta(minutes=settings.session_idle_timeout_minutes)
        if row is None or row.expires_at <= now or row.last_seen_at <= idle_cutoff:
            if row is not None:
                db.delete(row)
                db.commit()
            return None
        touch_cutoff = now - timedelta(seconds=settings.session_touch_interval_seconds)
        if row.last_seen_at <= touch_cutoff:
            row.last_seen_at = now
            db.commit()
        return {"sub": row.sub, "name": row.name, "email": row.email, "username": row.username,
                "groups": json.loads(row.groups_json), "local": row.local}
    finally:
        db.close()


def delete_session_cookie(token: str | None) -> None:
    if not token:
        return
    db = SessionLocal()
    try:
        row = db.get(models.UserSession, _session_hash(token))
        if row:
            db.delete(row)
            db.commit()
    finally:
        db.close()


def purge_expired_sessions() -> int:
    now = utcnow()
    idle_cutoff = now - timedelta(minutes=settings.session_idle_timeout_minutes)
    db = SessionLocal()
    try:
        count = db.query(models.UserSession).filter(
            (models.UserSession.expires_at <= now) | (models.UserSession.last_seen_at <= idle_cutoff)
        ).delete(synchronize_session=False)
        db.commit()
        return count
    finally:
        db.close()


def get_current_user_optional(request) -> dict | None:
    state = getattr(request, "state", None)
    if state is not None and hasattr(state, "nbm_current_user"):
        return state.nbm_current_user
    user = read_session_cookie(request.cookies.get(SESSION_COOKIE))
    if state is not None:
        state.nbm_current_user = user
    return user


def get_current_actor(request) -> dict:
    """
    Identity to attribute an action to, for audit logging. Falls back to an
    explicit "anonymous" marker when authentication is disabled, so audit entries
    are never silently missing a value — it's obvious from the record itself
    that auth was off rather than that logging failed.
    """
    user = get_current_user_optional(request)
    if user:
        return {
            "sub": user.get("sub"),
            "name": user.get("name"),
            "email": user.get("email"),
            "username": user.get("username"),
        }
    return {"sub": None, "name": "anonymous (auth disabled)", "email": None, "username": None}


# --- OIDC client ---
# The OIDC client is created only when authentication is enabled and the full
# OIDC configuration is present.
oauth = None
if not settings.authentication_disabled and settings.oidc_enabled:
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name="oidc",
        server_metadata_url=f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        client_kwargs={"scope": settings.oidc_scopes},
    )
