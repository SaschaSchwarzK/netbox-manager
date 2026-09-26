"""
Session handling and the OIDC client registry.

We don't keep server-side session state: the session cookie itself carries
the user's identity (sub/email/name/groups) signed with itsdangerous, so any
backend replica can validate it without a shared session store. The cookie
is httponly and (in production) secure+samesite=lax, so it can't be read or
sent cross-site by JS, only presented back to us by the browser.
"""
import hmac
import time

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

SESSION_COOKIE = "nbm_session"
SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours

_serializer = URLSafeTimedSerializer(settings.session_secret_key, salt="nbm-session")
_local_login_failures: dict[str, list[float]] = {}
_LOCAL_LOGIN_MAX_FAILURES = 10
_LOCAL_LOGIN_WINDOW_SECONDS = 5 * 60


def create_session_cookie(user: dict) -> str:
    return _serializer.dumps(user)


def create_local_session_cookie(username: str) -> str:
    return create_session_cookie(
        {
            "sub": f"local:{username}",
            "email": None,
            "name": f"local admin ({username})",
            "groups": [],
            "local": True,
        }
    )


def check_local_credentials(username: str, password: str) -> bool:
    username_matches = hmac.compare_digest(username, settings.local_admin_user)
    password_matches = hmac.compare_digest(password, settings.local_admin_password)
    return settings.local_admin_enabled and username_matches and password_matches


def _recent_local_failures(client_host: str) -> list[float]:
    cutoff = time.monotonic() - _LOCAL_LOGIN_WINDOW_SECONDS
    failures = [stamp for stamp in _local_login_failures.get(client_host, []) if stamp > cutoff]
    if failures:
        _local_login_failures[client_host] = failures
    else:
        _local_login_failures.pop(client_host, None)
    return failures


def local_login_retry_after(client_host: str) -> int | None:
    failures = _recent_local_failures(client_host)
    if len(failures) < _LOCAL_LOGIN_MAX_FAILURES:
        return None
    return max(1, int(_LOCAL_LOGIN_WINDOW_SECONDS - (time.monotonic() - failures[0])))


def record_local_login_failure(client_host: str) -> None:
    failures = _recent_local_failures(client_host)
    failures.append(time.monotonic())
    _local_login_failures[client_host] = failures


def clear_local_login_failures(client_host: str) -> None:
    _local_login_failures.pop(client_host, None)


def read_session_cookie(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        return _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def get_current_user_optional(request) -> dict | None:
    return read_session_cookie(request.cookies.get(SESSION_COOKIE))


def get_current_actor(request) -> dict:
    """
    Identity to attribute an action to, for audit logging. Falls back to an
    explicit "anonymous" marker when OIDC isn't configured, so audit entries
    are never silently missing a value — it's obvious from the record itself
    that auth was off rather than that logging failed.
    """
    user = get_current_user_optional(request)
    if user:
        return {"sub": user.get("sub"), "name": user.get("name"), "email": user.get("email")}
    return {"sub": None, "name": "anonymous (auth disabled)", "email": None}


# --- OIDC client ---
# `oauth` stays None when NBM_OIDC_ISSUER isn't set, so the app can still run
# (with auth disabled) for local development without a real identity provider.
oauth = None
if settings.oidc_issuer:
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name="oidc",
        server_metadata_url=f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        client_kwargs={"scope": settings.oidc_scopes},
    )
