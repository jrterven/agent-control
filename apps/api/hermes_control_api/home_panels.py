"""Explicit previews share the durable owner lease with scheduled analysis."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from .home import digest, panel_digest, layout_for, owned_resources, utc, validate_sources
from .home_models import HomePanelSnapshot, HomePreference, HomeReview
from .home_schemas import HomeLayout
from .home_sources import HomeUnavailable, collect_sources
from .home_inference import analyze, panel_payload


def acquire(db, owner, now, *, scheduled=False):
    row = db.get(HomePreference, owner)
    if row is None:
        db.add(HomePreference(owner_id=owner, revision=0, layout=HomeLayout().model_dump(by_alias=True), next_review_at=now))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    token = str(uuid4())
    conditions = [HomePreference.owner_id == owner,
        or_(HomePreference.lease_until.is_(None), HomePreference.lease_until <= now)]
    if scheduled:
        conditions.extend([HomePreference.next_review_at <= now,
            or_(HomePreference.last_analysis_at.is_(None), HomePreference.last_analysis_at <= now - timedelta(hours=1))])
    else:
        conditions.append(or_(HomePreference.last_analysis_at.is_(None), HomePreference.last_analysis_at <= now - timedelta(minutes=1)))
    result = db.execute(update(HomePreference).where(*conditions).execution_options(synchronize_session=False).values(lease_id=token, lease_until=now + timedelta(minutes=10)))
    db.commit()
    if result.rowcount != 1:
        return None
    db.expire_all()
    return token


def save_snapshot(db, services, owner, panel, sources, generated, *, now):
    row = db.scalar(select(HomePanelSnapshot).where(HomePanelSnapshot.owner_id == owner, HomePanelSnapshot.panel_id == panel.id))
    if row is None:
        row = HomePanelSnapshot(owner_id=owner, panel_id=panel.id)
        db.add(row)
    payload = panel_payload(generated, sources)
    row.definition_digest = panel_digest(panel)
    row.source_digest = digest(sources)
    row.content_ciphertext = services.vault.encrypt(json.dumps(payload), aad=f'home-panel:{owner}:{panel.id}')
    row.error_code = None
    row.updated_at = now
    row.checked_at = now
    return {**payload, 'status': 'ready', 'updatedAt': now}


async def preview_panel(db, services, owner, panel, language):
    now = datetime.now(timezone.utc)
    layout = layout_for(db, owner)
    if len(layout.panels) >= 20 or panel.kind != 'custom' or not panel.profile_id:
        raise HTTPException(422, 'HOME_INVALID_PANEL')
    if panel.id in {p.id for p in layout.panels}:
        raise HTTPException(409, 'HOME_PANEL_EXISTS')
    resources = owned_resources(db, owner, services.settings.deployment_mode == 'cloud')
    validate_sources(panel.sources, resources)
    if panel.profile_id not in {p.id for p in resources['profiles']}:
        raise HTTPException(422, 'HOME_AGENT_UNAVAILABLE')
    token = acquire(db, owner, now)
    if not token:
        raise HTTPException(409, 'HOME_ANALYSIS_BUSY')
    review = HomeReview(owner_id=owner, started_at=now, status='preview')
    db.add(review)
    db.commit()
    try:
        sources = await asyncio.wait_for(collect_sources(db, services, owner, panel.sources), 90)
        row = db.get(HomePreference, owner)
        row.last_analysis_at = datetime.now(timezone.utc)
        db.commit()
        result, usage = await analyze(db, services, owner, panel.profile_id,
            {'language': language, 'sources': sources, 'panels': [panel.model_dump(by_alias=True)], 'suggestions': False}, review.id)
        db.expire_all()
        row = db.get(HomePreference, owner)
        if row.lease_id != token or row.revision != layout.revision:
            raise HomeUnavailable('HOME_LAYOUT_CHANGED')
        validate_sources(panel.sources, owned_resources(db, owner, services.settings.deployment_mode == 'cloud'))
        panel = panel.model_copy(update={'title': panel.title or result.panels[0].title})
        snapshot = save_snapshot(db, services, owner, panel, sources, result.panels[0], now=datetime.now(timezone.utc))
        review.status = 'preview_ready'
        review.input_tokens, review.output_tokens = usage['inputTokens'], usage['outputTokens']
        db.commit()
        return {'definition': panel.model_dump(by_alias=True), 'snapshot': snapshot}
    except Exception as exc:
        review.status = 'failed'
        review.error_code = exc.code if isinstance(exc, HomeUnavailable) else 'HOME_SOURCE_UNAVAILABLE'
        db.commit()
        raise HTTPException(409, review.error_code) from None
    finally:
        review.finished_at = datetime.now(timezone.utc)
        db.execute(update(HomePreference).where(HomePreference.owner_id == owner, HomePreference.lease_id == token).values(lease_id=None, lease_until=None))
        db.commit()
