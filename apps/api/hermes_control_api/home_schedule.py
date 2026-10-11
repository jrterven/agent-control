"""Hourly owner leases, change detection and durable daily suggestion policy."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import re
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from .home import digest, panel_digest, layout_for, owned_resources, utc, validate_sources
from .home_inference import analyze
from .home_jobs_models import HomeNotice
from .home_models import HomePanelSnapshot, HomePreference, HomeReview, HomeSuggestion
from .home_panels import acquire, save_snapshot
from .home_sources import collect_sources, HomeUnavailable
from .models import User


def silent(preference, now):
    if not preference.quiet_start:
        return False
    current = now.astimezone(ZoneInfo(preference.timezone)).strftime('%H:%M')
    start, end = preference.quiet_start, preference.quiet_end
    return start <= current < end if start < end else current >= start or current < end


def verified_deadline(value, text, zone):
    """Only exact source timestamps can enable the urgency exception.

    Ambiguous natural-language deadlines may appear in ordinary suggestions,
    but cannot authorize additional reminders.
    """
    if not value:
        return None
    try:
        candidate = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if candidate.tzinfo is None:
            return None
        # ISO date+time in the retrieved evidence; a date alone is insufficient.
        for match in re.finditer(r'\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?', text):
            token = match.group()
            prefix = text[max(0, match.start() - 100):match.start()].lower()
            if not re.search(r'\b(due|deadline|vence|vencimiento|entrega|antes de|fecha l[ií]mite)\b', prefix):
                continue
            stated = datetime.fromisoformat(token.replace('Z', '+00:00'))
            if stated.tzinfo is None:
                local_zone = ZoneInfo(zone)
                if stated.replace(tzinfo=local_zone, fold=0).utcoffset() != stated.replace(tzinfo=local_zone, fold=1).utcoffset():
                    continue
                stated = stated.replace(tzinfo=local_zone)
            if stated == candidate:
                return candidate.astimezone(timezone.utc)
    except ValueError:
        pass
    return None


def due_bucket(row, now):
    if row.status != 'open' or (row.snoozed_until and utc(row.snoozed_until) > now):
        return 'inactive'
    if not row.due_at:
        return 'open'
    remaining = utc(row.due_at) - now
    return 'overdue' if remaining <= timedelta(0) else 'urgent' if remaining <= timedelta(hours=2) else 'open'


def read_content(services, owner, row):
    return json.loads(services.vault.decrypt(row.content_ciphertext, aad=f'home-suggestion:{owner}:{row.identity_key}'))


def write_content(services, owner, row, payload):
    row.content_ciphertext = services.vault.encrypt(json.dumps(payload), aad=f'home-suggestion:{owner}:{row.identity_key}')


def apply_suggestions(db, services, owner, preference, generated, sources, now):
    indexed = {s['key']: s for s in sources}
    for proposal in generated:
        source = indexed.get(proposal.source_key)
        if source is None or proposal.quote not in source['text'] or not proposal.quote.strip():
            continue
        # Identity comes from the source/thread and a backend-owned action, not
        # model wording or invented IDs. Rephrasing updates the same card.
        identity = digest([source['key'], source['action']])
        row = db.scalar(select(HomeSuggestion).where(HomeSuggestion.owner_id == owner, HomeSuggestion.identity_key == identity))
        if row and (row.status != 'open' or (row.snoozed_until and utc(row.snoozed_until) > now)):
            continue
        changed = row is None or row.evidence_digest != source.get('sourceDigest', digest(source['text']))
        previous = read_content(services, owner, row) if row else {}
        if row is None:
            row = HomeSuggestion(owner_id=owner, identity_key=identity, source_key=source['key'], action_key=source['action'], status='open')
            db.add(row)
        # Reuse unchanged prepared work instead of accepting gratuitous rewrites.
        draft = proposal.draft if changed else previous.get('draft', proposal.draft)
        payload = {'title': proposal.title, 'reason': proposal.reason, 'draft': draft,
            'sourceKey': source['key'], 'profileId': preference.profile_id,
            'noticePending': changed or previous.get('noticePending', False),
            'evidence': {'quote': proposal.quote, **{k: source[k] for k in ('title', 'url', 'sessionId') if source.get(k)}}}
        row.priority = proposal.priority
        row.evidence_digest = source.get('sourceDigest', digest(source['text']))
        row.due_at = verified_deadline(proposal.due_at, source['text'], preference.timezone)
        row.snoozed_until = None
        row.updated_at = now
        write_content(services, owner, row, payload)
    db.flush()


def notices(db, services, owner, preference, now, allowed_keys=None):
    if not preference.enabled or silent(preference, now):
        return []
    today = now.astimezone(ZoneInfo(preference.timezone)).date().isoformat()
    emitted = []
    for row in db.scalars(select(HomeSuggestion).where(HomeSuggestion.owner_id == owner, HomeSuggestion.status == 'open').order_by(HomeSuggestion.priority.desc()).limit(100)):
        if (row.snoozed_until and utc(row.snoozed_until) > now) or (allowed_keys is not None and row.source_key not in allowed_keys):
            continue
        payload = read_content(services, owner, row)
        recorded = set(db.scalars(select(HomeNotice.kind).where(HomeNotice.owner_id == owner, HomeNotice.suggestion_id == row.id, HomeNotice.day == today)))
        kind = None
        if payload.get('noticePending') and 'regular' not in recorded:
            kind = 'regular'
        elif ('regular' in recorded and 'urgent' not in recorded and due_bucket(row, now) == 'urgent'
                and row.last_notified_at and now - utc(row.last_notified_at) >= timedelta(hours=1)):
            kind = 'urgent'
        if kind:
            db.add(HomeNotice(owner_id=owner, suggestion_id=row.id, day=today, kind=kind, created_at=now))
            row.last_notified_at = now
            row.notified_day = today
            if kind == 'urgent':
                row.urgent_day = today
            emitted.append({'id': row.id, 'kind': kind, 'dueAt': row.due_at.isoformat() if row.due_at else None})
        # Updates that already had today's reminder stay silent on the next day.
        payload['noticePending'] = False
        write_content(services, owner, row, payload)
        if len(emitted) == 5:
            break
    return emitted


class HomeScheduler:
    def __init__(self, services, *, draining=lambda: False, clock=lambda: datetime.now(timezone.utc)):
        self.services = services
        self.draining = draining
        self.clock = clock
        self.active = 0

    async def tick(self):
        if self.draining():
            return
        now = self.clock()
        with self.services.session_factory() as db:
            owners = list(db.scalars(select(HomePreference.owner_id).join(User, User.id == HomePreference.owner_id).where(
                User.is_active.is_(True), HomePreference.next_review_at <= now,
                or_(HomePreference.lease_until.is_(None), HomePreference.lease_until <= now),
                or_(HomePreference.last_analysis_at.is_(None), HomePreference.last_analysis_at <= now - timedelta(hours=1))).order_by(HomePreference.next_review_at).limit(8)))
        capacity = asyncio.Semaphore(4)
        async def run(owner):
            async with capacity:
                if self.draining():
                    return
                self.active += 1
                try:
                    # Whole-check bound is below the durable ten-minute lease,
                    # even with many slow panel sources. Never outlive ownership.
                    await asyncio.wait_for(self.review(owner), timeout=480)
                except TimeoutError:
                    pass  # review cancellation persists its interrupted state.
                finally:
                    self.active -= 1
        await asyncio.gather(*(run(owner) for owner in owners))

    async def review(self, owner):
        now = self.clock()
        services = self.services
        with services.session_factory() as db:
            token = acquire(db, owner, now, scheduled=True)
            if not token:
                return
            row = db.get(HomePreference, owner)
            layout = layout_for(db, owner)
            review = HomeReview(owner_id=owner, started_at=now, status='checking')
            db.add(review)
            row.next_review_at = now + timedelta(hours=1)
            row.last_checked_at = now
            db.commit()
            try:
                if layout.paused or not (layout.proactive.enabled or any(p.kind == 'custom' for p in layout.panels)):
                    review.status = 'paused'
                    return
                if row.last_error == 'HOME_MODEL_UNAVAILABLE':
                    review.status = 'suspended'
                    return
                # Read selected inputs only. Group identical selections once.
                cache = {}
                async def collect(sources):
                    key = digest(sources.model_dump())
                    if key not in cache:
                        cache[key] = await asyncio.wait_for(collect_sources(db, services, owner, sources), 90)
                    return cache[key]
                jobs = []
                proactive_sources = []
                proactive_digest = None
                stored = list(db.scalars(select(HomeSuggestion).where(HomeSuggestion.owner_id == owner).limit(100)))
                if layout.proactive.enabled:
                    proactive_sources = await collect(layout.proactive.sources)
                    buckets = [(s.identity_key, due_bucket(s, now)) for s in stored]
                    proactive_digest = digest([proactive_sources, sorted(buckets)])
                    if proactive_sources and proactive_digest != row.source_digest:
                        jobs.append((layout.proactive.profile_id, 'suggestions', None, proactive_sources))
                for panel in layout.panels:
                    if panel.kind != 'custom':
                        continue
                    snapshot = db.scalar(select(HomePanelSnapshot).where(HomePanelSnapshot.owner_id == owner, HomePanelSnapshot.panel_id == panel.id))
                    if snapshot and utc(snapshot.checked_at or snapshot.updated_at) + timedelta(minutes=panel.refresh_minutes) > now:
                        continue
                    sources = await collect(panel.sources)
                    if snapshot and snapshot.definition_digest == panel_digest(panel) and snapshot.source_digest == digest(sources):
                        snapshot.checked_at = now
                        snapshot.error_code = None
                    else:
                        jobs.append((panel.profile_id, 'panel', panel, sources))
                # One model call per owner per hour. After proactive changes, the
                # oldest due panel group is served; other agents remain queued.
                if jobs:
                    # Fairness across explicitly selected agents: use the oldest
                    # last result. This never exceeds one analysis per user/hour.
                    def last_result(job):
                        if job[1] == 'suggestions':
                            return utc(row.last_proactive_at) or datetime.min.replace(tzinfo=timezone.utc)
                        snapshot = db.scalar(select(HomePanelSnapshot).where(HomePanelSnapshot.owner_id == owner, HomePanelSnapshot.panel_id == job[2].id))
                        return utc(snapshot.updated_at) if snapshot else datetime.min.replace(tzinfo=timezone.utc)
                    jobs.sort(key=last_result)
                    selected_profile = jobs[0][0]
                    selected_jobs = [j for j in jobs if j[0] == selected_profile][:4]
                    panels = [j[2] for j in selected_jobs if j[1] == 'panel']
                    include_suggestions = any(j[1] == 'suggestions' for j in selected_jobs)
                    unique = {s['key']: s for j in selected_jobs for s in j[3]}
                    actionable = {s['key'] for s in proactive_sources} if include_suggestions else set()
                    data = [{**s, 'actionable': s['key'] in actionable} for s in unique.values()]
                    # Distribute a fixed text budget over all selected sources;
                    # evidence must still quote this bounded supplied text.
                    per_source = min(1800, 24000 // max(1, len(data)))
                    data = [{**s, 'sourceDigest': digest(s['text']), 'text': s['text'][:per_source]} for s in data]
                    panel_specs = [{'id': p.id, 'title': p.title, 'description': p.description, 'display': p.display, 'sourceKeys': [s['key'] for j in selected_jobs if j[2] == p for s in j[3]]} for p in panels]
                    previous = [{'sourceKey': s.source_key, 'status': s.status,
                        'snoozedUntil': s.snoozed_until.isoformat() if s.snoozed_until else None,
                        'draft': read_content(services, owner, s).get('draft', '')[:600]} for s in stored[:20]]
                    if self.draining():
                        review.status = 'deferred'
                        return
                    row.last_analysis_at = self.clock()
                    db.commit()  # Cost gate persists BEFORE the RPC, including crashes.
                    result, usage = await analyze(db, services, owner, selected_profile,
                        {'language': layout.proactive.language, 'sources': [{k: s[k] for k in ('key', 'title', 'text', 'actionable')} for s in data],
                         'panels': panel_specs, 'suggestions': include_suggestions,
                         'previous': previous, 'now': now.isoformat()}, review.id)
                    db.expire_all()
                    row = db.get(HomePreference, owner)
                    if row.lease_id != token or row.revision != layout.revision:
                        review.status = 'superseded'
                        return
                    resources = owned_resources(db, owner, services.settings.deployment_mode == 'cloud')
                    if selected_profile not in {p.id for p in resources['profiles']} or not db.get(User, owner).is_active:
                        review.status = 'superseded'
                        return
                    for panel in panels:
                        validate_sources(panel.sources, resources)
                        generated = next(p for p in result.panels if p.id == panel.id)
                        save_snapshot(db, services, owner, panel, next(j[3] for j in selected_jobs if j[2] == panel), generated, now=now)
                    if include_suggestions:
                        validate_sources(layout.proactive.sources, resources)
                        apply_suggestions(db, services, owner, layout.proactive, result.suggestions, [s for s in data if s["key"] in actionable], now)
                        row.last_proactive_at = now
                        # Store buckets AFTER publication, avoiding a second call
                        # caused solely by newly persisted card identities.
                        current = list(db.scalars(select(HomeSuggestion).where(HomeSuggestion.owner_id == owner).limit(100)))
                        row.source_digest = digest([proactive_sources, sorted((s.identity_key, due_bucket(s, now)) for s in current)])
                    review.input_tokens, review.output_tokens = usage['inputTokens'], usage['outputTokens']
                    review.status = 'ready'
                else:
                    review.status = 'unchanged'
                row.last_error = None
                emitted = notices(db, services, owner, layout.proactive, now, {s["key"] for s in proactive_sources})
                db.commit()  # Daily receipts precede notification delivery; never replay on restart.
                if emitted and services.push_notifications:
                    await services.push_notifications.send_home(owner, emitted, layout.proactive.language)
            except asyncio.CancelledError:
                review.status = 'interrupted'
                row.last_error = 'HOME_SOURCE_UNAVAILABLE'
                raise
            except Exception as exc:
                db.rollback()
                row = db.get(HomePreference, owner)
                review = db.get(HomeReview, review.id)
                review.status = 'failed'
                review.error_code = exc.code if isinstance(exc, HomeUnavailable) else 'HOME_SOURCE_UNAVAILABLE'
                row.last_error = review.error_code
                # Offline checks consolidate at reconnection; no hourly backlog.
                if row.last_analysis_at is None or utc(row.last_analysis_at) <= now - timedelta(hours=1):
                    row.next_review_at = now + timedelta(minutes=1)
                for snapshot in db.scalars(select(HomePanelSnapshot).where(HomePanelSnapshot.owner_id == owner)):
                    snapshot.error_code = review.error_code
            finally:
                review.finished_at = self.clock()
                db.execute(update(HomePreference).where(HomePreference.owner_id == owner, HomePreference.lease_id == token).values(lease_id=None, lease_until=None))
                db.commit()
