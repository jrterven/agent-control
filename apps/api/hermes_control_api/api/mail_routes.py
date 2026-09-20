from __future__ import annotations

import asyncio
import base64
from datetime import timedelta
import hashlib
import hmac
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..auth import SESSION_COOKIE, aware, current_user, get_db, require_csrf, resolve_session
from ..mail_models import MailAccount, MailAgent, MailGrant, MailOAuthFlow
from ..mail_providers import MailError, http_json, identity, imap_work
from ..mail_schemas import MailAccountInput, MailAccountUpdate, MailOAuthStart, address
from ..mail_service import OAUTH
from ..models import AuthSession, User, new_id, utc_now
from ..security import random_token, token_hash
from ..services import audit

router = APIRouter(prefix="/api/v1/mail", tags=["mail"])


def service(request):
    return request.app.state.mail_service


def record(db, user, action, target=None):
    audit(db, actor=user, action="mail." + action, target_type="mail_account", target_id=target, details={})
    db.commit()


@router.get("/providers")
def providers(request: Request, _: User = Depends(current_user)):
    return [{"id": provider, "enabled": service(request).enabled(provider)} for provider in ("gmail", "outlook", "hostinger", "imap")]


@router.get("/accounts")
def accounts(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(MailAccount).where(MailAccount.owner_id == user.id).order_by(MailAccount.created_at)).all()
    return [service(request).view(db, row) for row in rows]


@router.post("/accounts", dependencies=[Depends(require_csrf)])
async def connect_account(payload: MailAccountInput, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    svc = service(request)
    if not svc.enabled(payload.provider):
        raise MailError("MAIL_PROVIDER_DISABLED", 409)
    if payload.account_id:
        svc.account(db, user.id, payload.account_id)
    if payload.provider == "hostinger" and payload.service == "custom":
        raise MailError("MAIL_INVALID_CONFIGURATION", 422)
    domain = {"hostinger": "hostinger.com", "titan": "titan.email"}.get(payload.service)
    config = {"address": payload.address, "username": payload.username, "service": payload.service,
              "imapHost": "imap." + domain if domain else payload.imap_host,
              "smtpHost": "smtp." + domain if domain else payload.smtp_host,
              "smtpPort": payload.smtp_port}
    secret = {"password": payload.password.get_secret_value()}
    async with svc.lock("owner:" + user.id):
        with svc.operation(user.id):
            await asyncio.to_thread(imap_work, config, secret, "test", {})
        identifier = config["imapHost"].lower() + "\n" + payload.username.lower()
        row = svc.save(db, user.id, payload.provider, identifier, payload.address, secret, config, payload.label, payload.account_id)
        record(db, user, "connected", row.id)
        return svc.view(db, row)


@router.patch("/accounts/{account_id}", dependencies=[Depends(require_csrf)])
async def update_account(account_id: str, payload: MailAccountUpdate, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    svc = service(request)
    async with svc.lock("owner:" + user.id):
        row = svc.account(db, user.id, account_id)
        row.label = payload.label.strip() or row.address
        svc.assign(db, user, row, payload.profile_ids)
        record(db, user, "updated", row.id)
        return svc.view(db, row)


@router.post("/accounts/{account_id}/test", dependencies=[Depends(require_csrf)])
async def test_account(account_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    svc = service(request)
    row = svc.account(db, user.id, account_id)
    with svc.operation(user.id):
        await svc.test(db, row)
    agents = db.scalars(select(MailAgent).join(MailGrant, MailGrant.agent_id == MailAgent.id).where(MailGrant.account_id == row.id, MailAgent.owner_id == user.id)).all()
    for agent in agents:
        if agent.state != "ready":
            await svc.provision(db, agent)
    record(db, user, "tested", row.id)
    return svc.view(db, row)


@router.delete("/accounts/{account_id}", status_code=204, dependencies=[Depends(require_csrf)])
async def disconnect_account(account_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    svc = service(request)
    async with svc.lock("owner:" + user.id):
        row = svc.account(db, user.id, account_id)
        # Retire in-flight reconnect flows as well: a late callback cannot restore access.
        db.execute(delete(MailOAuthFlow).where(MailOAuthFlow.owner_id == user.id, MailOAuthFlow.account_id == row.id))
        db.delete(row)
        record(db, user, "disconnected", account_id)


@router.post("/oauth/{provider}/start", dependencies=[Depends(require_csrf)])
async def oauth_start(provider: str, payload: MailOAuthStart, request: Request, auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    svc = service(request)
    with svc.operation(auth.user_id):
        pass
    if provider not in OAUTH or not svc.enabled(provider):
        raise MailError("MAIL_PROVIDER_DISABLED", 409)
    if payload.account_id and svc.account(db, auth.user_id, payload.account_id).provider != provider:
        raise MailError("MAIL_DIFFERENT_ACCOUNT", 409)
    state, browser, verifier = random_token(), random_token(), random_token()
    flow_id = new_id()
    db.execute(delete(MailOAuthFlow).where(MailOAuthFlow.expires_at < utc_now()))
    db.add(MailOAuthFlow(id=flow_id, owner_id=auth.user_id, session_id=auth.id, provider=provider,
        state_hash=token_hash(state), browser_hash=token_hash(browser),
        verifier_ciphertext=svc.vault.encrypt(verifier, aad="mail-oauth:" + flow_id),
        account_id=str(payload.account_id) if payload.account_id else None, expires_at=utc_now() + timedelta(minutes=10)))
    db.commit()
    parameters = {"client_id": getattr(svc.settings, f"mail_{provider}_client_id"), "response_type": "code",
        "redirect_uri": svc.settings.public_base_url + f"/api/v1/mail/oauth/{provider}/callback",
        "scope": OAUTH[provider]["scope"], "state": state, "code_challenge_method": "S256",
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
    if provider == "gmail":
        parameters.update(access_type="offline", prompt="consent select_account")
    else:
        parameters.update(prompt="select_account", response_mode="query")
    response = JSONResponse({"authorizationUrl": OAUTH[provider]["authorize"] + "?" + urlencode(parameters)})
    response.set_cookie("hc_mail_" + provider, browser, secure=svc.settings.secure_cookies, httponly=True, samesite="lax", max_age=600, path="/api/v1/mail/oauth/" + provider)
    return response


@router.get("/oauth/{provider}/callback")
async def oauth_callback(provider: str, request: Request, db: Session = Depends(get_db)):
    svc, outcome = service(request), "failed"
    try:
        if provider not in OAUTH or not svc.enabled(provider):
            raise MailError()
        state, code = request.query_params.get("state", ""), request.query_params.get("code", "")
        if not 1 <= len(state) <= 256 or not 1 <= len(code) <= 8192:
            raise MailError()
        flow = db.scalar(select(MailOAuthFlow).where(MailOAuthFlow.state_hash == token_hash(state), MailOAuthFlow.provider == provider))
        browser = request.cookies.get("hc_mail_" + provider, "")
        if not flow or flow.consumed_at or aware(flow.expires_at) <= utc_now() or not browser or not hmac.compare_digest(flow.browser_hash, token_hash(browser)):
            raise MailError()
        auth = db.get(AuthSession, flow.session_id)
        current = resolve_session(db, request.cookies.get(SESSION_COOKIE))
        if not auth or auth.revoked_at or aware(auth.expires_at) <= utc_now() or not auth.user.is_active or (current and current.user_id != flow.owner_id):
            raise MailError()
        consumed = db.execute(update(MailOAuthFlow).where(MailOAuthFlow.id == flow.id, MailOAuthFlow.consumed_at.is_(None)).values(consumed_at=utc_now()))
        if consumed.rowcount != 1:
            raise MailError()
        db.commit()
        verifier = svc.vault.decrypt(flow.verifier_ciphertext, aad="mail-oauth:" + flow.id)
        token = await http_json("POST", OAUTH[provider]["token"], data={"grant_type": "authorization_code", "code": code,
            "client_id": getattr(svc.settings, f"mail_{provider}_client_id"), "client_secret": getattr(svc.settings, f"mail_{provider}_client_secret"),
            "redirect_uri": svc.settings.public_base_url + f"/api/v1/mail/oauth/{provider}/callback", "code_verifier": verifier})
        if not token.get("refresh_token") or not token.get("access_token"):
            raise MailError()
        scopes = {scope.rsplit("/", 1)[-1].lower() for scope in token.get("scope", "").split()}
        required = {"gmail.readonly", "gmail.send"} if provider == "gmail" else {"mail.read", "mail.send"}
        if not required.issubset(scopes):
            raise MailError()
        external, email = await identity(provider, token["access_token"])
        email = address(email)
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
        async with svc.lock("owner:" + flow.owner_id):
            # Re-read after provider I/O in case this flow/session was disconnected.
            db.expire_all()
            if db.get(MailOAuthFlow, flow.id) is None or db.get(AuthSession, flow.session_id).revoked_at:
                raise MailError()
            row = svc.save(db, flow.owner_id, provider, external, email, token, account_id=flow.account_id)
            record(db, auth.user, "connected", row.id)
        outcome = "connected"
    except Exception:
        db.rollback()
    response = RedirectResponse(f"/settings?mailResult={outcome}#plugins", status_code=303)
    if provider in OAUTH:
        response.delete_cookie("hc_mail_" + provider, path="/api/v1/mail/oauth/" + provider)
    return response
