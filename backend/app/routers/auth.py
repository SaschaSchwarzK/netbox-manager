from datetime import datetime

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
    get_current_user_optional,
    local_login_retry_after,
    oauth,
    record_local_login_failure,
)
from app.config import settings
from app.database import get_db
from app.rbac import get_access_context, AccessContext

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/login")
async def login(request: Request):
    if not oauth:
        raise HTTPException(503, "OIDC is not configured on this server (set NBM_OIDC_ISSUER and friends).")
    return await oauth.oidc.authorize_redirect(request, settings.oidc_redirect_uri)


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
    groups = userinfo.get(settings.oidc_groups_claim, [])
    if isinstance(groups, str):
        groups = [groups]

    for g in groups:
        seen = db.get(models.SeenOidcGroup, g)
        if seen:
            seen.last_seen_at = datetime.utcnow()
        else:
            db.add(models.SeenOidcGroup(name=g))
    db.commit()

    user = {
        "sub": userinfo.get("sub"),
        "email": userinfo.get("email"),
        "name": userinfo.get("name") or userinfo.get("preferred_username") or userinfo.get("email") or userinfo.get("sub"),
        "groups": groups,
    }

    response = RedirectResponse(url="/")
    response.set_cookie(
        SESSION_COOKIE,
        create_session_cookie(user),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.post("/local-login")
async def local_login(payload: schemas.LocalLoginRequest, request: Request):
    if not settings.local_admin_enabled:
        raise HTTPException(403, "Local admin login is not configured.")
    client_host = request.client.host if request.client else "unknown"
    retry_after = local_login_retry_after(client_host)
    if retry_after is not None:
        raise HTTPException(
            429,
            "Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    if not check_local_credentials(payload.username, payload.password):
        record_local_login_failure(client_host)
        raise HTTPException(401, "Invalid credentials.")

    clear_local_login_failures(client_host)
    user = {
        "sub": f"local:{payload.username}",
        "email": None,
        "name": f"local admin ({payload.username})",
        "groups": [],
        "local": True,
    }
    response = JSONResponse({"ok": True, "user": user, "role": "admin"})
    response.set_cookie(
        SESSION_COOKIE,
        create_local_session_cookie(payload.username),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return response


@router.get("/me")
async def me(request: Request, ctx: AccessContext = Depends(get_access_context)):
    user = get_current_user_optional(request)
    if not settings.auth_required:
        return {
            "auth_enabled": False,
            "oidc_enabled": oauth is not None,
            "local_login_enabled": settings.local_admin_enabled,
            "authenticated": True,
            "user": None,
            "role": ctx.role,
            "app_admin": ctx.app_admin,
        }
    return {
        "auth_enabled": settings.auth_required,
        "oidc_enabled": oauth is not None,
        "local_login_enabled": settings.local_admin_enabled,
        "authenticated": user is not None,
        "user": user,
        "role": ctx.role,
        "app_admin": ctx.app_admin,
    }
