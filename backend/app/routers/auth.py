
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    check_local_credentials,
    clear_local_login_failures,
    create_local_session_cookie,
    create_session_cookie,
    delete_session_cookie,
    get_current_user_optional,
    local_login_retry_after,
    normalize_groups,
    oauth,
    record_local_login_failure,
)
from app.config import settings, startup_configuration_errors
from app.database import get_db
from app.rbac import get_access_context, require_app_admin, AccessContext
from app.timeutil import utcnow

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/login")
async def login(request: Request):
    errors = startup_configuration_errors()
    if errors:
        raise HTTPException(503, {"code": "configuration_error", "message": "Application configuration is invalid.", "errors": errors})
    if not oauth:
        raise HTTPException(503, "OIDC is not configured on this server (set NBM_OIDC_ISSUER and friends).")
    try:
        return await oauth.oidc.authorize_redirect(request, settings.oidc_redirect_uri)
    except Exception as exc:
        raise HTTPException(502, f"OIDC provider discovery failed: {exc}") from exc


@router.get("/callback")
async def callback(request: Request, db: Session = Depends(get_db)):
    if not oauth:
        raise HTTPException(503, "OIDC is not configured on this server.")
    try:
        token = await oauth.oidc.authorize_access_token(request)
    except Exception as exc:
        raise HTTPException(400, f"OIDC login failed: {exc}")

    # authlib validates the ID token's signature (against the provider's JWKS),
    # issuer, audience, expiry, and nonce as part of authorize_access_token();
    # `userinfo` below is that already-verified claim set.
    userinfo = token.get("userinfo") or {}
    groups = normalize_groups(userinfo.get(settings.oidc_groups_claim, []))

    for g in groups:
        seen = db.get(models.SeenOidcGroup, g)
        if seen:
            seen.last_seen_at = utcnow()
        else:
            db.add(models.SeenOidcGroup(name=g))
    db.commit()

    user = {
        "sub": userinfo.get("sub"),
        "email": userinfo.get("email"),
        "name": userinfo.get("name") or userinfo.get("preferred_username") or userinfo.get("email") or userinfo.get("sub"),
        "username": userinfo.get("preferred_username"),
        "groups": groups,
    }
    request.state.nbm_audit_actor = user
    request.state.nbm_audit_event = {
        "action": "login", "resource_type": "authentication",
        "detail": "Logged in using OpenID Connect.",
    }

    response = RedirectResponse(url="/")
    response.set_cookie(
        SESSION_COOKIE,
        create_session_cookie(user, db),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    request.state.nbm_audit_event = {
        "action": "logout", "resource_type": "authentication", "detail": "Logged out.",
    }
    delete_session_cookie(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.post("/local-login")
async def local_login(payload: schemas.LocalLoginRequest, request: Request, db: Session = Depends(get_db)):
    if settings.authentication_disabled or not settings.local_admin_enabled:
        raise HTTPException(403, "Local admin login is not configured.")
    client_host = request.client.host if request.client else "unknown"
    request.state.nbm_audit_actor = {
        "sub": None,
        "email": None,
        "name": f"local admin login attempt ({payload.username})",
    }
    request.state.nbm_audit_event = {
        "action": "login", "resource_type": "authentication",
        "detail": f"Attempted break-glass login as {payload.username}.",
    }
    retry_after = local_login_retry_after(client_host, payload.username)
    if retry_after is not None:
        raise HTTPException(
            429,
            "Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    if not check_local_credentials(payload.username, payload.password):
        record_local_login_failure(client_host, payload.username)
        raise HTTPException(401, "Invalid credentials.")

    clear_local_login_failures(client_host, payload.username)
    user = {
        "sub": f"local:{payload.username}",
        "email": None,
        "name": f"local admin ({payload.username})",
        "groups": [],
        "local": True,
    }
    request.state.nbm_audit_actor = user
    request.state.nbm_audit_event["detail"] = "Logged in using the break-glass administrator account."
    response = JSONResponse({"ok": True, "user": user, "role": "admin"})
    response.set_cookie(
        SESSION_COOKIE,
        create_local_session_cookie(payload.username, db),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return response


@router.delete("/sessions")
def revoke_sessions(sub: str, db: Session = Depends(get_db), _ctx: AccessContext = Depends(require_app_admin)):
    revoked = db.query(models.UserSession).filter(models.UserSession.sub == sub).delete(synchronize_session=False)
    db.commit()
    return {"revoked": revoked}


@router.get("/me")
async def me(request: Request, ctx: AccessContext = Depends(get_access_context)):
    user = get_current_user_optional(request)
    errors = startup_configuration_errors()
    if settings.authentication_disabled:
        return {
            "auth_enabled": False,
            "oidc_enabled": False,
            "local_login_enabled": False,
            "authenticated": True,
            "user": None,
            "role": ctx.role,
            "app_admin": ctx.app_admin,
        }
    return {
        "auth_enabled": True,
        "oidc_enabled": oauth is not None and not errors,
        "local_login_enabled": settings.local_admin_enabled and not errors,
        "authenticated": user is not None,
        "user": user,
        "role": ctx.role,
        "app_admin": ctx.app_admin,
        "configuration_errors": errors,
    }
