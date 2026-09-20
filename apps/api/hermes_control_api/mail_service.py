from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import time
from weakref import WeakValueDictionary

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from hermes_client.compatibility import HERMES_0212_SHA

from .admin_service import AdminResourceService
from .auth import aware
from .connector_models import Connector
from .mail_models import MailAccount, MailAgent, MailGrant, MailOAuthFlow, MailSendOperation
from .mail_providers import MailError, http_json, identity, imap_work, read_message, search_messages, send_message
from .models import Gateway, ProfileRef, User, new_id, utc_now
from .security import random_token, token_hash
from .services import GatewayService


OAUTH = {
    "gmail": {"authorize": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
              "scope": "openid email https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send"},
    "outlook": {"authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize", "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                "scope": "openid email offline_access User.Read Mail.Read Mail.Send"},
}


class MailService:
    def __init__(self, services):
        self.services, self.settings, self.vault = services, services.settings, services.vault
        self.locks = WeakValueDictionary()
        self.budgets = OrderedDict()
        self.active = {}

    @contextmanager
    def operation(self, owner_id):
        """Bound provider work without queuing an unbounded number of requests."""
        now = time.monotonic()
        while self.budgets and next(iter(self.budgets.values()))[0] < now - 60:
            self.budgets.popitem(last=False)
        start, count = self.budgets.get(owner_id, (now, 0))
        if count >= 60 or self.active.get(owner_id, 0) >= 4 or sum(self.active.values()) >= 16 or len(self.budgets) >= 4096:
            raise MailError("MAIL_RATE_LIMITED", 429)
        self.budgets[owner_id] = (start, count + 1)
        self.active[owner_id] = self.active.get(owner_id, 0) + 1
        try:
            yield
        finally:
            self.active[owner_id] -= 1
            if not self.active[owner_id]:
                del self.active[owner_id]

    def lock(self, key):
        lock = self.locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[key] = lock
        return lock

    def enabled(self, provider):
        base = self.settings.public_base_url or ""
        if not base.startswith("https://"):
            return False
        if provider in {"imap", "hostinger"}:
            return self.settings.mail_imap_enabled
        return provider in OAUTH and all(getattr(self.settings, f"mail_{provider}_{key}") for key in ("enabled", "client_id", "client_secret"))

    def seal(self, account, secret):
        account.credential_ciphertext = self.vault.encrypt(json.dumps(secret), aad=f"mail:{account.owner_id}:{account.id}")

    def account(self, db, owner_id, account_id):
        row = db.scalar(select(MailAccount).where(MailAccount.id == str(account_id), MailAccount.owner_id == owner_id))
        if row is None:
            raise MailError("MAIL_ACCOUNT_NOT_FOUND", 404)
        return row

    def route(self, db, owner_id, profile_id):
        row = db.get(ProfileRef, profile_id)
        gateway = db.get(Gateway, row.gateway_id) if row else None
        if not row or not gateway or not gateway.enabled:
            raise MailError("MAIL_AGENT_UNAVAILABLE", 409)
        if self.settings.deployment_mode == "cloud":
            connector = db.scalar(select(Connector).where(Connector.gateway_id == gateway.id, Connector.owner_id == owner_id))
            if gateway.owner_id != owner_id or not connector or connector.revoked_at or row.profile_name not in connector.profiles:
                raise MailError("MAIL_AGENT_UNAVAILABLE", 409)
        return row, gateway

    def view(self, db, row):
        agents = db.scalars(select(MailAgent).join(MailGrant, MailGrant.agent_id == MailAgent.id).where(MailGrant.account_id == row.id, MailAgent.owner_id == row.owner_id)).all()
        return {"id": row.id, "provider": row.provider, "address": row.address, "label": row.label, "status": row.status,
                "config": row.config if row.provider in {"imap", "hostinger"} else {},
                "agents": [{"profileId": agent.profile_id, "state": agent.state} for agent in agents]}

    def save(self, db, owner_id, provider, external_id, address, secret, config=None, label=None, account_id=None):
        digest = hashlib.sha256(external_id.encode()).hexdigest()
        row = db.scalar(select(MailAccount).where(MailAccount.owner_id == owner_id, MailAccount.provider == provider, MailAccount.identity == digest))
        if account_id:
            expected = self.account(db, owner_id, account_id)
            if expected.provider != provider or expected.identity != digest:
                raise MailError("MAIL_DIFFERENT_ACCOUNT", 409)
            row = expected
        if row is None:
            if db.scalar(select(func.count()).select_from(MailAccount).where(MailAccount.owner_id == owner_id)) >= 32:
                raise MailError("MAIL_ACCOUNT_LIMIT", 409)
            row = MailAccount(id=new_id(), owner_id=owner_id, provider=provider, identity=digest, address=address, label=label or address, config=config or {})
            db.add(row)
        row.address, row.status = address, "connected"
        if config is not None:
            row.config = config
        if label:
            row.label = label
        self.seal(row, secret)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise MailError("MAIL_CONNECTION_CHANGED", 409) from None
        return row

    def assign(self, db, owner, row, profile_ids):
        if self.settings.deployment_mode != "cloud" and not owner.is_admin:
            raise MailError("MAIL_AGENT_FORBIDDEN", 403)
        ids = set(map(str, profile_ids))
        for identifier in ids:
            self.route(db, owner.id, identifier)
        db.execute(delete(MailGrant).where(MailGrant.account_id == row.id))
        for identifier in ids:
            agent = db.scalar(select(MailAgent).where(MailAgent.owner_id == owner.id, MailAgent.profile_id == identifier))
            if agent is None:
                token, aid = random_token(), new_id()
                agent = MailAgent(id=aid, owner_id=owner.id, profile_id=identifier, token_hash=token_hash(token),
                    token_ciphertext=self.vault.encrypt(token, aad=f"mail-agent:{owner.id}:{aid}"), state="pending")
                db.add(agent)
                db.flush()
            elif agent.state != "ready":
                agent.state, agent.last_attempt_at = "pending", None
            db.add(MailGrant(account_id=row.id, agent_id=agent.id))
        db.commit()

    async def credentials(self, db, account):
        if not self.enabled(account.provider):
            raise MailError("MAIL_PROVIDER_DISABLED", 409)
        async with self.lock(account.id):
            db.refresh(account)
            try:
                secret = json.loads(self.vault.decrypt(account.credential_ciphertext, aad=f"mail:{account.owner_id}:{account.id}"))
            except (ValueError, TypeError):
                raise MailError("MAIL_RECONNECT_REQUIRED", 409) from None
            if account.provider not in OAUTH or secret.get("expires_at", 0) > time.time() + 90:
                return secret
            try:
                updated = await http_json("POST", OAUTH[account.provider]["token"], data={
                    "grant_type": "refresh_token", "refresh_token": secret["refresh_token"],
                    "client_id": getattr(self.settings, f"mail_{account.provider}_client_id"),
                    "client_secret": getattr(self.settings, f"mail_{account.provider}_client_secret"),
                })
                if not updated.get("access_token"):
                    raise MailError()
                secret = {**secret, **updated, "expires_at": time.time() + int(updated.get("expires_in", 3600))}
                self.seal(account, secret)
                account.status = "connected"
                db.commit()
                return secret
            except (MailError, KeyError, ValueError):
                account.status = "reconnect_required"
                db.commit()
                raise MailError("MAIL_RECONNECT_REQUIRED", 409) from None

    async def test(self, db, account):
        secret = await self.credentials(db, account)
        try:
            if account.provider in OAUTH:
                await identity(account.provider, secret["access_token"])
            else:
                await asyncio.to_thread(imap_work, account.config, secret, "test", {})
            account.status = "connected"
            db.commit()
        except MailError as exc:
            if exc.code == "MAIL_RECONNECT_REQUIRED":
                account.status = "reconnect_required"
                db.commit()
            raise

    async def provision(self, db, agent):
        async with self.lock("agent:" + agent.id):
            agent.last_attempt_at = utc_now()
            actor = db.get(User, agent.owner_id)
            if not actor or not actor.is_active or (self.settings.deployment_mode != "cloud" and not actor.is_admin):
                agent.state = "unavailable"
                db.commit()
                return
            has_grants = db.scalar(select(MailGrant.account_id).where(MailGrant.agent_id == agent.id).limit(1)) is not None
            if not has_grants and agent.state == "idle":
                return
            try:
                profile, gateway = self.route(db, actor.id, agent.profile_id)
                connection = await GatewayService(self.services).connection(db, gateway.id, profile.profile_name)
                if connection.trusted_source_sha != HERMES_0212_SHA and self.settings.provider_mode != "mock":
                    agent.state = "unsupported"
                    db.commit()
                    return
                provider = await AdminResourceService(self.services).provider(db, gateway_id=gateway.id, profile_name=profile.profile_name, capability="mcp.create" if has_grants else "mcp.delete")
                name = "agent_control_mail_" + agent.id.replace("-", "")
                url = (self.settings.public_base_url or "").rstrip("/") + "/api/v1/mail/mcp"
                listing = await provider.list_mcp_servers()
                existing = next((s for s in listing.data.get("servers", []) if s.get("name") == name), None)
                if existing and existing.get("url") != url:
                    agent.state = "conflict"
                    db.commit()
                    return
                if not has_grants:
                    if existing:
                        await provider.delete_mcp_server(name)
                    # Exact native key derived solely from our server UUID.
                    await provider.delete_secret("MCP_" + name.upper() + "_API_KEY")
                    agent.state = "idle"
                else:
                    if not existing:
                        token = self.vault.decrypt(agent.token_ciphertext, aad=f"mail-agent:{actor.id}:{agent.id}")
                        await provider.create_mcp_server({"name": name, "url": url, "auth": "header", "bearer_token": token})
                    elif existing.get("enabled") is False:
                        await provider.toggle_mcp_server(name, True)
                    result = await provider.test_mcp_server(name)
                    agent.state = "ready" if result.data.get("ok") is True else "setup_required"
            except Exception:
                # Do not expose native exceptions: they may contain local paths or secrets.
                agent.state = "pending" if has_grants else "pending_removal"
            agent.last_attempt_at = utc_now()
            db.commit()

    async def reconcile(self, app):
        while True:
            await asyncio.sleep(15)
            if app.state.cloud_draining:
                continue
            try:
                await self.reconcile_once(app)
            except Exception:
                # Retry transient database/connector failures; never log secrets.
                continue

    async def reconcile_once(self, app):
        with app.state.session_factory() as db:
            db.execute(delete(MailOAuthFlow).where(MailOAuthFlow.expires_at < utc_now()))
            db.commit()
            # Ready rows must not starve pending rows behind a fixed limit.
            agents = list(db.scalars(select(MailAgent).order_by(MailAgent.last_attempt_at)))
            for agent in agents:
                if app.state.cloud_draining:
                    break
                has_grant = db.scalar(select(MailGrant.account_id).where(MailGrant.agent_id == agent.id).limit(1))
                if (agent.state == "ready" and has_grant) or (agent.state == "idle" and not has_grant):
                    continue
                if agent.last_attempt_at and aware(agent.last_attempt_at) > utc_now() - timedelta(seconds=60):
                    continue
                app.state.cloud_mutations_inflight += 1
                try:
                    await self.provision(db, agent)
                finally:
                    app.state.cloud_mutations_inflight -= 1

    async def execute(self, db, agent, name, args):
        self.route(db, agent.owner_id, agent.profile_id)
        if name == "mail_accounts":
            rows = db.scalars(select(MailAccount).join(MailGrant, MailGrant.account_id == MailAccount.id).where(MailGrant.agent_id == agent.id, MailAccount.owner_id == agent.owner_id)).all()
            return [{"accountId": row.id, "address": row.address, "label": row.label, "provider": row.provider, "status": row.status} for row in rows]
        account = self.account(db, agent.owner_id, args.account_id)
        if db.get(MailGrant, (account.id, agent.id)) is None:
            raise MailError("MAIL_ACCOUNT_FORBIDDEN", 403)
        secret = await self.credentials(db, account)
        # Refresh can await provider I/O. Revocation must win before a mail operation.
        db.expire_all()
        self.route(db, agent.owner_id, agent.profile_id)
        if db.scalar(select(MailGrant.account_id).where(MailGrant.account_id == account.id, MailGrant.agent_id == agent.id)) is None:
            raise MailError("MAIL_ACCOUNT_FORBIDDEN", 403)
        if name == "mail_search":
            return await search_messages(account, secret, args.text, args.limit)
        if name == "mail_read":
            return await read_message(account, secret, args.message_id)
        digest = hashlib.sha256(args.model_dump_json().encode()).hexdigest()
        previous = db.scalar(select(MailSendOperation).where(MailSendOperation.owner_id == agent.owner_id, MailSendOperation.operation_id == str(args.operation_id)))
        if previous:
            if previous.digest != digest:
                raise MailError("MAIL_SEND_CONFLICT", 409)
            return {"status": previous.status, "operationId": previous.operation_id, "retry": False}
        operation = MailSendOperation(owner_id=agent.owner_id, account_id=account.id, operation_id=str(args.operation_id), digest=digest, status="delivery_unknown")
        db.add(operation)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise MailError("MAIL_SEND_IN_PROGRESS", 409) from None
        try:
            await send_message(account, secret, args)
        except Exception:
            raise MailError("MAIL_DELIVERY_UNKNOWN", 409) from None
        operation.status = "accepted"
        db.commit()
        return {"status": "accepted", "operationId": operation.operation_id, "retry": False}
