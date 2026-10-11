from copy import deepcopy
from sqlalchemy import select

from hermes_control_api.models import User, Workspace
from hermes_control_api.security import hash_password
from .conftest import mutation_headers


def test_home_layout_persists_and_rejects_stale_device(authenticated):
    client, csrf = authenticated
    home = client.get('/api/v1/home')
    assert home.status_code == 200, home.text
    layout = home.json()['layout']
    assert [p['kind'] for p in layout['panels']] == ['pending', 'prepared', 'background', 'automations']
    stale = deepcopy(layout)
    layout['panels'].reverse()
    layout['panels'][0].update(pinned=True, size='wide')
    layout.update(background='dusk', reduceTransparency=True)
    assert client.put('/api/v1/home/layout', json=layout).status_code == 403
    saved = client.put('/api/v1/home/layout', json=layout, headers=mutation_headers(csrf, 'home-save'))
    assert saved.status_code == 200, saved.text
    assert saved.json()['layout']['revision'] == 1
    assert client.get('/api/v1/home').json()['layout'] == saved.json()['layout']
    assert client.put('/api/v1/home/layout', json=stale, headers=mutation_headers(csrf, 'home-conflict')).status_code == 409


def test_home_sources_and_configuration_are_owner_scoped(authenticated, app):
    client, csrf = authenticated
    with app.state.session_factory() as db:
        other = User(username='other-home', password_hash=hash_password('some other secure pass'))
        db.add(other)
        db.flush()
        workspace = Workspace(owner_id=other.id, name='Private project')
        db.add(workspace)
        db.commit()
        workspace_id = workspace.id
    result = client.get('/api/v1/home').json()
    assert workspace_id not in [w['id'] for w in result['sources']['workspaces']]
    result['layout']['panels'][0]['sources']['workspaceIds'] = [workspace_id]
    response = client.put('/api/v1/home/layout', json=result['layout'], headers=mutation_headers(csrf, 'foreign-home'))
    assert response.status_code == 422
    assert client.get('/api/v1/home').json()['layout']['revision'] == 0


def test_home_rejects_invalid_schema_and_duplicate_panels(authenticated):
    client, csrf = authenticated
    original = client.get('/api/v1/home').json()['layout']
    for index, mutate in enumerate([
        lambda p: p['panels'].append(p['panels'][0]),
        lambda p: p['proactive'].update(timezone='Invalid/Nowhere'),
        lambda p: p['proactive'].update(quietStart='25:00'),
        lambda p: p['panels'][0]['sources'].update(urls=['https://127.0.0.1/secret']),
        lambda p: p['panels'][0].update(code='sendMail()'),
    ]):
        candidate = deepcopy(original)
        mutate(candidate)
        assert client.put('/api/v1/home/layout', json=candidate, headers=mutation_headers(csrf, f'invalid-home-{index}')).status_code == 422


def test_custom_preview_is_data_only_and_preserved_on_save(authenticated, app, monkeypatch):
    from unittest.mock import AsyncMock
    from hermes_control_api import home_panels
    from hermes_control_api.home_inference import InsightResult
    from hermes_control_api.home_models import HomePanelSnapshot
    client, csrf = authenticated
    client.get('/api/v1/bootstrap')
    home = client.get('/api/v1/home').json()
    profile = home['sources']['profiles'][0]['id']
    generated = InsightResult.model_validate({'panels': [{'id': 'custom-test', 'title': 'My research', 'blocks': [{'type': 'list', 'items': ['No new findings']}]}]})
    analysis = AsyncMock(return_value=(generated, {'inputTokens': 12, 'outputTokens': 7}))
    monkeypatch.setattr(home_panels, 'collect_sources', AsyncMock(return_value=[]))
    monkeypatch.setattr(home_panels, 'analyze', analysis)
    panel = {**home['layout']['panels'][0], 'id': 'custom-test', 'kind': 'custom', 'title': '', 'description': 'My research', 'profileId': profile}
    response = client.post('/api/v1/home/panels/preview', json={'panel': panel}, headers=mutation_headers(csrf, 'preview-home'))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['snapshot']['blocks'][0]['items'] == ['No new findings']
    home['layout']['panels'].append(result['definition'])
    saved = client.put('/api/v1/home/layout', json=home['layout'], headers=mutation_headers(csrf, 'save-preview'))
    assert saved.status_code == 200, saved.text
    assert saved.json()['snapshots']['custom-test']['blocks'] == result['snapshot']['blocks']
    assert analysis.call_count == 1
    with app.state.session_factory() as db:
        stored = db.scalar(select(HomePanelSnapshot))
        assert 'No new findings' not in stored.content_ciphertext


def test_connector_without_restricted_analysis_never_receives_a_prompt(authenticated, app, monkeypatch):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from hermes_control_api import home_inference
    client, csrf = authenticated
    client.get('/api/v1/bootstrap')
    home = client.get('/api/v1/home').json()
    profile = home['sources']['profiles'][0]['id']
    provider = SimpleNamespace(capabilities=AsyncMock(return_value=SimpleNamespace(methods=set())), submit_prompt=AsyncMock())
    monkeypatch.setattr(home_inference, 'provider_for', AsyncMock(return_value=provider))
    panel = {**home['layout']['panels'][0], 'id': 'restricted-test', 'kind': 'custom', 'description': 'Summarize safely', 'profileId': profile}
    response = client.post('/api/v1/home/panels/preview', json={'panel': panel}, headers=mutation_headers(csrf, 'old-connector'))
    assert response.status_code == 409, response.text
    assert 'HOME_CONNECTOR_UPDATE_REQUIRED' in response.text
    provider.submit_prompt.assert_not_called()
