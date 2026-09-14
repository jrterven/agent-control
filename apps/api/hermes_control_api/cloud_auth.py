"""Invitation-only Google OIDC authorization code flow with PKCE.

Only fixed Google endpoints are contacted. ID tokens and access tokens are
never retained; flow secrets expire after ten minutes and are single use.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from datetime import timedelta
from urllib.parse import urlencode, unquote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from .auth import SESSION_COOKIE, aware, get_db, issue_session
from .config import Settings
from .models import BetaInvitation, ExternalIdentity, OIDCFlow, User, new_id, utc_now
from .security import hash_password, random_token, token_hash

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
FLOW_COOKIE = "hc_oidc_browser"
ISSUER = "https://accounts.google.com"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"


def safe_return_to(value: str | None) -> str:
    decoded = unquote(value or "")
    if (value and len(value) <= 2048 and decoded.startswith("/")
        and not decoded.startswith("//") and "\\" not in decoded
        and not any(ord(c) < 32 or ord(c) == 127 for c in decoded)
        and not decoded.startswith("/api/")):
        return value
    return "/chats"


def normalized_email(value: str) -> str:
    email = value.strip().casefold()
    if len(email) > 320 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("A valid email is required")
    return email


def google_enabled(settings: Settings) -> bool:
    return bool(settings.deployment_mode == "cloud" and settings.public_base_url
                and settings.google_client_id and settings.google_client_secret)


def lock_enrollment(db: Session) -> None:
    # Serialize enrollment and invitations across CLI/API processes. The fixed
    # application lock is released automatically with the transaction.
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(7294172601)"))


def invite_email(db: Session, settings: Settings, email: str, *, days: int = 14) -> BetaInvitation:
    email = normalized_email(email)
    lock_enrollment(db)
    now = utc_now()
    row = db.scalar(select(BetaInvitation).where(BetaInvitation.email == email))
    if row is not None and row.accepted_at is not None:
        raise ValueError("This email has already joined the beta")
    users = db.scalar(select(func.count()).select_from(ExternalIdentity)) or 0
    pending = db.scalar(select(func.count()).select_from(BetaInvitation).where(
        BetaInvitation.accepted_at.is_(None), BetaInvitation.revoked_at.is_(None),
        BetaInvitation.expires_at > now, BetaInvitation.email != email,
    )) or 0
    if users + pending >= settings.beta_max_users:
        raise ValueError("The beta invitation limit has been reached")
    if row is None:
        row = BetaInvitation(email=email, expires_at=now + timedelta(days=days))
        db.add(row)
    row.expires_at = now + timedelta(days=days)
    row.revoked_at = None
    db.commit()
    return row


def enroll_google_identity(db: Session, settings: Settings, claims: dict) -> User:
    if claims.get("email_verified") is not True:
        raise ValueError("invite_required")
    email = normalized_email(str(claims.get("email", "")))
    subject = claims.get("sub")
    if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
        raise ValueError("google_login_failed")
    lock_enrollment(db)
    identity = db.scalar(select(ExternalIdentity).where(
        ExternalIdentity.issuer == ISSUER, ExternalIdentity.subject == subject,
    ))
    if identity is not None:
        user = db.get(User, identity.user_id)
        if user is None or not user.is_active:
            raise ValueError("google_login_failed")
        # Stable issuer/sub wins; email changes do not create or merge accounts.
        identity.email = email
        db.commit()
        return user
    invitation = db.scalar(select(BetaInvitation).where(BetaInvitation.email == email).with_for_update())
    if (invitation is None or invitation.revoked_at is not None
        or invitation.accepted_at is not None or aware(invitation.expires_at) <= utc_now()):
        raise ValueError("invite_required")
    if (db.scalar(select(func.count()).select_from(ExternalIdentity)) or 0) >= settings.beta_max_users:
        raise ValueError("beta_full")
    # Never merge a local password account by matching its display name/email.
    display_name = email[:120]
    if db.scalar(select(User.id).where(User.username == display_name)) is not None:
        display_name = f"{email[:80]}-{new_id()}"
    user = User(id=new_id(), username=display_name, password_hash=hash_password(random_token()), is_admin=False)
    db.add(user)
    db.flush()
    db.add(ExternalIdentity(user_id=user.id, issuer=ISSUER, subject=subject, email=email))
    invitation.user_id = user.id
    invitation.accepted_at = utc_now()
    db.commit()
    return user


async def exchange_google_code(settings: Settings, code: str, verifier: str, nonce: str) -> dict:
    from authlib.integrations.httpx_client import AsyncOAuth2Client
    async with AsyncOAuth2Client(
        settings.google_client_id, settings.google_client_secret,
        redirect_uri=f"{settings.public_base_url}/api/v1/auth/google/callback",
        token_endpoint_auth_method="client_secret_post", timeout=10, trust_env=False,
    ) as client:
        token = await client.fetch_token(TOKEN_URL, code=code, code_verifier=verifier)
    id_token = token.get("id_token")
    if not isinstance(id_token, str) or len(id_token) > 32768:
        raise ValueError("google_login_failed")
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        response = await client.get(JWKS_URL)
        response.raise_for_status()
        jwks = response.json()
    return verify_google_id_token(settings, id_token, jwks, nonce)


def verify_google_id_token(settings: Settings, id_token: str, jwks: dict, nonce: str) -> dict:
    from authlib.jose import JsonWebToken
    claims = JsonWebToken(["RS256"]).decode(id_token, jwks, claims_options={
        "iss": {"essential": True, "values": [ISSUER, "accounts.google.com"]},
        "sub": {"essential": True}, "exp": {"essential": True}, "iat": {"essential": True},
        "aud": {"essential": True, "value": settings.google_client_id},
        "nonce": {"essential": True, "value": nonce},
    })
    claims.validate(leeway=30)
    if claims.get("azp", settings.google_client_id) != settings.google_client_id:
        raise ValueError("google_login_failed")
    if isinstance(claims.get("aud"), list) and len(claims["aud"]) > 1 and not claims.get("azp"):
        raise ValueError("google_login_failed")
    return dict(claims)


@router.get("/methods")
def methods(request: Request) -> dict:
    settings = request.app.state.services.settings
    return {"mode": settings.deployment_mode, "googleEnabled": google_enabled(settings)}


@router.get("/google/start")
def google_start(request: Request, return_to: str | None = Query(default=None, alias="returnTo"), db: Session = Depends(get_db)):
    settings = request.app.state.services.settings
    if not google_enabled(settings):
        raise HTTPException(404, "Google login is not configured")
    state, browser, nonce, verifier = (random_token() for _ in range(4))
    flow_id = new_id()
    db.execute(delete(OIDCFlow).where(OIDCFlow.expires_at < utc_now()))
    db.add(OIDCFlow(
        id=flow_id, state_hash=token_hash(state), browser_hash=token_hash(browser), nonce=nonce,
        verifier_ciphertext=request.app.state.services.vault.encrypt(verifier, aad=f"oidc:{flow_id}"),
        expires_at=utc_now() + timedelta(minutes=10), return_to=safe_return_to(return_to),
    ))
    db.commit()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    response = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": settings.google_client_id, "redirect_uri": f"{settings.public_base_url}/api/v1/auth/google/callback",
        "response_type": "code", "scope": "openid email profile", "state": state,
        "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
    }), status_code=302)
    response.set_cookie(FLOW_COOKIE, browser, secure=settings.secure_cookies,
                        httponly=True, samesite="lax", max_age=600, path="/api/v1/auth/google")
    return response


@router.get("/google/callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.services.settings
    if not google_enabled(settings):
        raise HTTPException(404, "Google login is not configured")
    failure = "google_login_failed"
    try:
        state, code = request.query_params.get("state", ""), request.query_params.get("code", "")
        if not state or len(state) > 256 or not code or len(code) > 8192:
            raise ValueError(failure)
        flow = db.scalar(select(OIDCFlow).where(OIDCFlow.state_hash == token_hash(state)))
        browser = request.cookies.get(FLOW_COOKIE, "")
        if (flow is None or not browser or flow.consumed_at is not None or aware(flow.expires_at) <= utc_now()
            or not hmac.compare_digest(flow.browser_hash, token_hash(browser))):
            raise ValueError(failure)
        result = db.execute(update(OIDCFlow).where(OIDCFlow.id == flow.id, OIDCFlow.consumed_at.is_(None)).values(consumed_at=utc_now()))
        if result.rowcount != 1:
            raise ValueError(failure)
        verifier = request.app.state.services.vault.decrypt(flow.verifier_ciphertext, aad=f"oidc:{flow.id}")
        db.commit()  # Consume before network I/O, including failed callbacks.
        claims = await exchange_google_code(settings, code, verifier, flow.nonce)
        user = enroll_google_identity(db, settings, claims)
        token, _, _ = issue_session(db, user, ttl_hours=settings.session_ttl_hours)
        response = RedirectResponse(safe_return_to(flow.return_to), status_code=303)
        response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=settings.secure_cookies,
                            samesite="strict", max_age=settings.session_ttl_hours * 3600, path="/")
    except (ValueError, IntegrityError, httpx.HTTPError) as exc:
        db.rollback()
        failure = str(exc) if str(exc) in {"invite_required", "beta_full"} else failure
        response = RedirectResponse(f"/login?error={failure}", status_code=303)
    except Exception:
        # Provider/library validation errors must not expose tokens or claims.
        db.rollback()
        response = RedirectResponse(f"/login?error={failure}", status_code=303)
    response.delete_cookie(FLOW_COOKIE, path="/api/v1/auth/google")
    return response
