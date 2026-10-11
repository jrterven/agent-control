"""Personal Home: explicit ownership, optimistic configuration, safe projections."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from .home_models import HomePanelSnapshot, HomePreference, HomeReview, HomeSuggestion
from .home_schemas import HomeLayout, HomeSources
from .mail_models import MailAccount
from .models import Automation, AutomationRun, Gateway, ProfileRef, SessionLink, Workspace
from .ownership import active_gateway_filter


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def layout_for(db, owner):
    row = db.get(HomePreference, owner)
    return HomeLayout.model_validate({**row.layout, "revision": row.revision}) if row else HomeLayout()


def owned_resources(db, owner, cloud):
    gateway_filter = active_gateway_filter(Gateway.id, cloud=cloud, owner_id=owner)
    gates = select(Gateway.id).where(Gateway.enabled.is_(True), gateway_filter)
    if cloud:
        gates = gates.where(Gateway.owner_id == owner)
    return {
        "sessions": list(db.scalars(select(SessionLink).where(SessionLink.owner_id == owner,
            SessionLink.gateway_id.in_(gates), SessionLink.chat_mode != "temporary", SessionLink.archived_at.is_(None))
            .order_by(SessionLink.updated_at.desc()).limit(500))),
        "workspaces": list(db.scalars(select(Workspace).where(Workspace.owner_id == owner, Workspace.archived_at.is_(None)))),
        "automations": list(db.scalars(select(Automation).where(Automation.owner_id == owner, Automation.gateway_id.in_(gates)))),
        "profiles": list(db.scalars(select(ProfileRef).where(ProfileRef.gateway_id.in_(gates)))),
        "mailAccounts": list(db.scalars(select(MailAccount).where(MailAccount.owner_id == owner))),
    }


def validate_sources(sources: HomeSources, resources):
    for field, key in (("session_ids", "sessions"), ("workspace_ids", "workspaces"),
                       ("automation_ids", "automations"), ("mail_account_ids", "mailAccounts")):
        if set(getattr(sources, field)) - {row.id for row in resources[key]}:
            raise HTTPException(422, "HOME_SOURCE_UNAVAILABLE")


def save_layout(db, owner, layout, cloud):
    if layout.proactive.enabled:
        raise HTTPException(409, "HOME_FEATURE_PENDING")
    resources = owned_resources(db, owner, cloud)
    profiles = {row.id for row in resources["profiles"]}
    validate_sources(layout.proactive.sources, resources)
    for panel in layout.panels:
        validate_sources(panel.sources, resources)
        if panel.profile_id and panel.profile_id not in profiles:
            raise HTTPException(422, "HOME_AGENT_UNAVAILABLE")
    if layout.proactive.profile_id and layout.proactive.profile_id not in profiles:
        raise HTTPException(422, "HOME_AGENT_UNAVAILABLE")
    if layout.proactive.sources.urls:
        raise HTTPException(422, "Public sources belong to panels")
    values = layout.model_dump(by_alias=True, exclude={"revision"})
    row = db.get(HomePreference, owner)
    try:
        if row is None:
            if layout.revision:
                raise HTTPException(409, "HOME_LAYOUT_CHANGED")
            row = HomePreference(owner_id=owner, revision=1, layout=values, next_review_at=datetime.now(timezone.utc))
            db.add(row)
            db.flush()
        else:
            result = db.execute(update(HomePreference).where(HomePreference.owner_id == owner,
                HomePreference.revision == layout.revision).values(layout=values, revision=layout.revision + 1,
                # Configuration changes invalidate in-flight publication, not the hourly cost gate.
                source_digest=None, last_error=None))
            if result.rowcount != 1:
                raise HTTPException(409, "HOME_LAYOUT_CHANGED")
        panel_ids = [p.id for p in layout.panels]
        db.execute(delete(HomePanelSnapshot).where(HomePanelSnapshot.owner_id == owner, HomePanelSnapshot.panel_id.not_in(panel_ids)))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "HOME_LAYOUT_CHANGED") from None
    return layout_for(db, owner)


def source_choices(resources):
    return {
        "sessions": [{"id": s.id, "label": s.display_title or s.title or "Chat", "workspaceId": s.workspace_id} for s in resources["sessions"]],
        "workspaces": [{"id": w.id, "label": w.name} for w in resources["workspaces"]],
        "automations": [{"id": a.id, "label": a.name} for a in resources["automations"]],
        "mailAccounts": [{"id": m.id, "label": m.label or m.address, "status": m.status} for m in resources["mailAccounts"]],
        "profiles": [{"id": p.id, "label": p.display_name, "available": "insights.generate" in p.capabilities.get("methods", [])} for p in resources["profiles"]],
    }


def home_view(db, owner, services):
    layout = layout_for(db, owner)
    resources = owned_resources(db, owner, services.settings.deployment_mode == "cloud")
    snapshots = {}
    for panel in layout.panels:
        items = []
        status = "ready"
        stamp = None
        if panel.kind == "automations":
            items = [{"id": a.id, "title": a.name, "detail": a.schedule, "status": "enabled" if a.enabled else "paused", "url": "/automations"} for a in resources["automations"]]
        elif panel.kind == "pinned":
            items = [{"id": s.id, "title": s.display_title or s.title or "Chat", "sessionId": s.id} for s in resources["sessions"] if s.pinned_at]
        elif panel.kind == "background":
            for s in resources["sessions"]:
                snapshot = s.background_tasks or {}
                observed = snapshot.get("observedAt")
                for task in snapshot.get("items", []):
                    if task.get("state") not in {"queued", "running", "failed", "unknown"}:
                        continue
                    items.append({"id": task["id"], "title": s.display_title or s.title or "Chat", "sessionId": s.id,
                        "status": task.get("state", "unknown"), "observedAt": observed})
        elif panel.kind == "reports":
            ids = [a.id for a in resources["automations"]]
            runs = list(db.scalars(select(AutomationRun).where(AutomationRun.automation_id.in_(ids)).order_by(AutomationRun.created_at.desc()).limit(10)))
            names = {a.id: a.name for a in resources["automations"]}
            items = [{"id": r.id, "title": names[r.automation_id], "sessionId": r.session_link_id,
                      "status": r.status, "observedAt": r.finished_at} for r in runs]
        elif panel.kind == "mail":
            # Mail contents are read only by the explicit source-selection worker.
            items = [{"id": m.id, "title": m.label or m.address, "status": m.status, "url": "/settings"} for m in resources["mailAccounts"]]
        elif panel.kind == "custom":
            row = db.scalar(select(HomePanelSnapshot).where(HomePanelSnapshot.owner_id == owner, HomePanelSnapshot.panel_id == panel.id))
            if row and row.definition_digest == digest(panel.model_dump(by_alias=True)):
                payload = json.loads(services.vault.decrypt(row.content_ciphertext, aad=f"home-panel:{owner}:{panel.id}"))
                snapshots[panel.id] = {**payload, "status": "stale" if row.error_code else "ready", "errorCode": row.error_code, "updatedAt": row.updated_at}
                continue
            status = "pending"
        snapshots[panel.id] = {"items": items[:20], "status": status, "updatedAt": stamp}
    suggestions = []
    now = datetime.now(timezone.utc)
    for row in db.scalars(select(HomeSuggestion).where(HomeSuggestion.owner_id == owner,
            HomeSuggestion.status == "open").order_by(HomeSuggestion.priority.desc(), HomeSuggestion.updated_at.desc()).limit(100)):
        if row.snoozed_until and utc(row.snoozed_until) > now:
            continue
        payload = json.loads(services.vault.decrypt(row.content_ciphertext, aad=f"home-suggestion:{owner}:{row.identity_key}"))
        suggestions.append({**payload, "id": row.id, "status": row.status, "dueAt": row.due_at,
            "updatedAt": row.updated_at, "unread": bool(row.last_notified_at and (not row.seen_at or utc(row.last_notified_at) > utc(row.seen_at)))})
    preference = db.get(HomePreference, owner)
    reviews = list(db.scalars(select(HomeReview).where(HomeReview.owner_id == owner).order_by(HomeReview.started_at.desc()).limit(5)))
    return {"features": {"customPanels": True, "proactive": False}, "layout": layout.model_dump(by_alias=True), "snapshots": snapshots, "suggestions": suggestions[:5],
        "sources": source_choices(resources), "review": {"model": "gpt-6-luna", "intervalMinutes": 60,
            "lastCheckedAt": preference.last_checked_at if preference else None,
            "lastAnalysisAt": preference.last_analysis_at if preference else None,
            "nextReviewAt": preference.next_review_at if preference else None,
            "errorCode": preference.last_error if preference else None,
            "history": [{"id": r.id, "status": r.status, "startedAt": r.started_at, "errorCode": r.error_code,
                "inputTokens": r.input_tokens, "outputTokens": r.output_tokens} for r in reviews]}}
