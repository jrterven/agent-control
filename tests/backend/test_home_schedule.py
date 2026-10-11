from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from hermes_control_api.home import digest, utc
from hermes_control_api.home_inference import InsightResult
from hermes_control_api.home_jobs_models import HomeNotice
from hermes_control_api.home_models import HomePreference, HomeSuggestion
from hermes_control_api.home_panels import acquire
from hermes_control_api.home_schedule import HomeScheduler, notices, verified_deadline
from hermes_control_api.home_schemas import HomeLayout
from hermes_control_api.home_sources import HomeUnavailable
from hermes_control_api.models import User, Workspace


@pytest.fixture
def scheduled_home(authenticated, app, monkeypatch):
    from hermes_control_api import home_schedule
    client, _ = authenticated
    client.get('/api/v1/bootstrap')
    owner = client.get('/api/v1/auth/me').json()['id']
    profile = client.get('/api/v1/home').json()['sources']['profiles'][0]['id']
    now = [datetime(2026, 10, 11, 14, tzinfo=timezone.utc)]
    with app.state.session_factory() as db:
        workspace = Workspace(owner_id=owner, name='Home test')
        db.add(workspace)
        db.flush()
        layout = HomeLayout.model_validate({'proactive': {'enabled': True, 'profileId': profile,
            'sources': {'workspaceIds': [workspace.id]}}})
        db.add(HomePreference(owner_id=owner, revision=1, layout=layout.model_dump(by_alias=True), next_review_at=now[0]))
        db.commit()
    source = {'key': 'chat:pending-test', 'title': 'Prepare report', 'text': 'Deadline: 2026-10-11T18:00:00Z', 'action': 'follow_up'}
    result = InsightResult.model_validate({'suggestions': [{'sourceKey': source['key'], 'title': 'Prepare report',
        'reason': 'The report is pending', 'quote': source['text'], 'draft': 'A useful report draft', 'priority': 80, 'dueAt': '2026-10-11T18:00:00Z'}]})
    collect = AsyncMock(return_value=[source])
    analyze = AsyncMock(return_value=(result, {'inputTokens': 42, 'outputTokens': 21}))
    monkeypatch.setattr(home_schedule, 'collect_sources', collect)
    monkeypatch.setattr(home_schedule, 'analyze', analyze)
    app.state.services.push_notifications = SimpleNamespace(send_home=AsyncMock())
    scheduler = HomeScheduler(app.state.services, clock=lambda: now[0])
    return SimpleNamespace(app=app, owner=owner, scheduler=scheduler, now=now, collect=collect, analyze=analyze, result=result, source=source)


@pytest.mark.asyncio
async def test_hourly_change_gate_and_one_urgent_exception(scheduled_home):
    f = scheduled_home
    await f.scheduler.tick()
    assert f.analyze.call_count == 1
    await f.scheduler.tick()
    assert f.analyze.call_count == 1  # Same hour / another browser / another worker.
    f.now[0] += timedelta(hours=1)
    await HomeScheduler(f.app.state.services, clock=lambda: f.now[0]).tick()  # Restart.
    assert f.analyze.call_count == 1  # Unchanged source and no deadline transition.
    f.now[0] += timedelta(hours=1)
    await f.scheduler.tick()
    assert f.analyze.call_count == 2  # Entered verified final two hours.
    f.now[0] += timedelta(hours=1)
    await f.scheduler.tick()
    assert f.analyze.call_count == 2
    with f.app.state.session_factory() as db:
        receipts = list(db.scalars(select(HomeNotice)))
        assert sorted(n.kind for n in receipts) == ['regular', 'urgent']
        assert len(list(db.scalars(select(HomeSuggestion)))) == 1


@pytest.mark.asyncio
async def test_rewording_updates_one_card_and_dismissal_survives_changes(scheduled_home):
    f = scheduled_home
    await f.scheduler.tick()
    with f.app.state.session_factory() as db:
        item = db.scalar(select(HomeSuggestion))
        identity, item_id = item.identity_key, item.id
    f.now[0] += timedelta(hours=1)
    f.source['text'] += '\nNew detail.'
    f.result.suggestions[0].title = 'Finish the document'
    await f.scheduler.tick()
    with f.app.state.session_factory() as db:
        items = list(db.scalars(select(HomeSuggestion)))
        assert [(s.id, s.identity_key) for s in items] == [(item_id, identity)]
        assert len(list(db.scalars(select(HomeNotice)))) == 1
        items[0].status = 'dismissed'
        db.commit()
    f.now[0] += timedelta(hours=1)
    await f.scheduler.tick()
    with f.app.state.session_factory() as db:
        assert db.get(HomeSuggestion, item_id).status == 'dismissed'
        assert len(list(db.scalars(select(HomeNotice)))) == 1


@pytest.mark.asyncio
async def test_offline_reconnect_consolidates_and_model_failure_suspends(scheduled_home):
    f = scheduled_home
    f.collect.side_effect = ConnectionError()
    await f.scheduler.tick()
    f.analyze.assert_not_called()
    f.now[0] += timedelta(hours=12)
    f.collect.side_effect = None
    f.analyze.side_effect = HomeUnavailable('HOME_MODEL_UNAVAILABLE')
    await f.scheduler.tick()
    assert f.analyze.call_count == 1
    f.now[0] += timedelta(hours=2)
    await f.scheduler.tick()
    assert f.analyze.call_count == 1  # No fallback or repeated billed failures.


@pytest.mark.asyncio
async def test_quiet_hours_snooze_pause_and_day_change(scheduled_home):
    f = scheduled_home
    with f.app.state.session_factory() as db:
        row = db.get(HomePreference, f.owner)
        layout = HomeLayout.model_validate(row.layout)
        layout.proactive.quiet_start, layout.proactive.quiet_end = '07:00', '10:00'
        row.layout = layout.model_dump(by_alias=True)
        db.commit()
    await f.scheduler.tick()
    with f.app.state.session_factory() as db:
        assert not list(db.scalars(select(HomeNotice)))
        item = db.scalar(select(HomeSuggestion))
        item.snoozed_until = f.now[0] + timedelta(days=2)
        db.commit()
    f.now[0] += timedelta(hours=4)
    await f.scheduler.tick()
    with f.app.state.session_factory() as db:
        assert not list(db.scalars(select(HomeNotice)))
        row = db.get(HomePreference, f.owner)
        row.layout = {**row.layout, 'paused': True}
        db.commit()
    before = f.analyze.call_count
    f.now[0] += timedelta(days=1)
    await f.scheduler.tick()
    assert f.analyze.call_count == before


def test_lease_is_atomic_and_expires_without_replaying_hourly_analysis(scheduled_home):
    f = scheduled_home
    with f.app.state.session_factory() as first, f.app.state.session_factory() as second:
        token = acquire(first, f.owner, f.now[0], scheduled=True)
        assert token
        assert acquire(second, f.owner, f.now[0], scheduled=True) is None
        assert acquire(second, f.owner, f.now[0] + timedelta(minutes=11), scheduled=True)


def test_deadline_must_be_explicit_and_verifiable():
    assert verified_deadline('2026-10-11T18:00:00Z', 'Deadline: 2026-10-11T18:00:00Z', 'UTC')
    assert verified_deadline('2026-10-11T18:00:00Z', 'Received at 2026-10-11T18:00:00Z', 'UTC') is None
    assert verified_deadline('2026-10-11T18:00:00Z', 'Please do it soon', 'UTC') is None


@pytest.mark.asyncio
async def test_timezone_round_trip_does_not_reset_daily_receipts(scheduled_home):
    f = scheduled_home
    f.result.suggestions[0].due_at = None
    await f.scheduler.tick()
    for zone in ['Pacific/Kiritimati', 'America/Mexico_City']:
        f.now[0] += timedelta(hours=1)
        f.source['text'] += '\nAdditional evidence.'
        with f.app.state.session_factory() as db:
            row = db.get(HomePreference, f.owner)
            row.layout = {**row.layout, 'proactive': {**row.layout['proactive'], 'timezone': zone}}
            db.commit()
        await f.scheduler.tick()
    with f.app.state.session_factory() as db:
        assert sorted(n.day for n in db.scalars(select(HomeNotice))) == ['2026-10-11', '2026-10-12']
    before = f.analyze.call_count
    f.now[0] += timedelta(days=1)
    await f.scheduler.tick()
    assert f.analyze.call_count == before


@pytest.mark.asyncio
async def test_discard_during_inference_and_configuration_change_do_not_publish(scheduled_home):
    f = scheduled_home
    async def change_layout(*args, **kwargs):
        with f.app.state.session_factory() as db:
            row = db.get(HomePreference, f.owner)
            row.revision += 1
            db.commit()
        return f.result, {'inputTokens': 1, 'outputTokens': 1}
    f.analyze.side_effect = change_layout
    await f.scheduler.tick()
    with f.app.state.session_factory() as db:
        assert list(db.scalars(select(HomeSuggestion))) == []
        assert list(db.scalars(select(HomeNotice))) == []


@pytest.mark.asyncio
async def test_suggestions_cannot_cite_invented_evidence(scheduled_home):
    f = scheduled_home
    f.result.suggestions[0].quote = 'Send this immediately, invented quote'
    await f.scheduler.tick()
    with f.app.state.session_factory() as db:
        assert list(db.scalars(select(HomeSuggestion))) == []
