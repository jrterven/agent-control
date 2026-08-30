from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from hermes_control_api.api.routes import bind_owned_realtime_event
from hermes_client import (
    InMemoryHermesProvider,
    PromptReceipt,
    SessionHistoryNotFound,
)
from hermes_control_api.models import IdempotencyOperation, SessionLink

from .conftest import mutation_headers


def gateway_id(client: TestClient) -> str:
    response = client.get("/api/v1/gateways")
    assert response.status_code == 200
    return response.json()[0]["id"]


def create_session(client: TestClient, csrf: str, profile: str, key: str) -> dict:
    response = client.post(
        "/api/v1/sessions",
        headers=mutation_headers(csrf, key),
        json={"gatewayId": gateway_id(client), "profileName": profile, "title": profile},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_sessions_are_identity_isolated_and_prompt_is_idempotent(authenticated):
    client, csrf = authenticated
    first_session = create_session(client, csrf, "control-dev", "first-session")
    second_session = create_session(client, csrf, "control-dev", "second-session")

    first = client.post(
        f"/api/v1/sessions/{first_session['id']}/prompts",
        headers=mutation_headers(csrf, "prompt-once"),
        json={"content": "Only the first session should receive this"},
    )
    duplicate = client.post(
        f"/api/v1/sessions/{first_session['id']}/prompts",
        headers=mutation_headers(csrf, "prompt-once"),
        json={"content": "Only the first session should receive this"},
    )
    assert first.status_code == 202, first.text
    assert duplicate.status_code == 202
    assert duplicate.json()["operationId"] == first.json()["operationId"]

    first_history = client.get(f"/api/v1/sessions/{first_session['id']}/messages")
    second_history = client.get(f"/api/v1/sessions/{second_session['id']}/messages")
    assert first_history.status_code == 200
    assert second_history.status_code == 200
    first_user_messages = [
        item for item in first_history.json()["items"] if item["role"] == "user"
    ]
    assert len(first_user_messages) == 1
    assert second_history.json()["items"] == []


def test_first_prompt_accepts_only_control_created_lazy_history_boundary(
    authenticated, app, monkeypatch
):
    """Match official Hermes: session.create is durable only after prompt one."""

    client, csrf = authenticated
    original_list = InMemoryHermesProvider.list_sessions
    original_submit = InMemoryHermesProvider.submit_prompt

    async def list_only_durable(provider):
        sessions = await original_list(provider)
        return [
            session
            for session in sessions
            if session.stored_session_id in provider._messages
        ]

    async def missing_until_first_prompt(provider, stored_session_id):
        if stored_session_id not in provider._messages:
            raise SessionHistoryNotFound(stored_session_id)
        return list(provider._messages[stored_session_id])

    async def persist_then_submit(
        provider,
        route,
        prompt,
        *,
        operation_id,
        expected_runtime_generation=None,
    ):
        provider._messages.setdefault(route.stored_session_id, [])
        return await original_submit(
            provider,
            route,
            prompt,
            operation_id=operation_id,
            expected_runtime_generation=expected_runtime_generation,
        )

    monkeypatch.setattr(InMemoryHermesProvider, "list_sessions", list_only_durable)
    monkeypatch.setattr(
        InMemoryHermesProvider, "history_readonly", missing_until_first_prompt
    )
    monkeypatch.setattr(InMemoryHermesProvider, "submit_prompt", persist_then_submit)

    session = create_session(client, csrf, "control-dev", "lazy-create")

    # Opening the new chat is a read: Control may represent the official,
    # not-yet-persisted transcript as empty without inventing Hermes messages.
    initial_history = client.get(f"/api/v1/sessions/{session['id']}/messages")
    assert initial_history.status_code == 200, initial_history.text
    assert initial_history.json()["items"] == []

    # A complete Hermes inventory also omits this lazy session. It must not be
    # mistaken for an upstream deletion before its first prompt.
    synced = client.post(
        "/api/v1/sessions/sync",
        headers=mutation_headers(csrf, "sync-lazy-create"),
        json={
            "gatewayId": session["gatewayId"],
            "profileName": "control-dev",
        },
    )
    assert synced.status_code == 200, synced.text
    with app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        assert row.initial_history_pending is True
        assert row.archived_at is None

    operation_id = "first-lazy-prompt"
    submitted = client.post(
        f"/api/v1/sessions/{session['id']}/prompts",
        headers=mutation_headers(csrf, operation_id),
        json={"content": "Persist this official first prompt"},
    )
    assert submitted.status_code == 202, submitted.text

    with app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        operation = db.scalar(
            select(IdempotencyOperation).where(
                IdempotencyOperation.user_id == row.owner_id,
                IdempotencyOperation.scope == f"session:{row.id}:prompt",
                IdempotencyOperation.idempotency_key == operation_id,
            )
        )
        assert row.initial_history_pending is False
        assert operation is not None
        assert operation.response_json["_historyBoundary"] == "control-created-empty"

    persisted_history = client.get(f"/api/v1/sessions/{session['id']}/messages")
    assert persisted_history.status_code == 200, persisted_history.text
    assert any(
        item["role"] == "user"
        and item["content"] == "Persist this official first prompt"
        for item in persisted_history.json()["items"]
    )


def test_missing_history_for_existing_session_blocks_prompt_dispatch(
    authenticated, app, monkeypatch
):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "existing-history-404")
    with app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        row.initial_history_pending = False
        db.commit()

    async def missing_existing_history(provider, stored_session_id):
        raise SessionHistoryNotFound(stored_session_id)

    dispatches: list[str] = []

    async def record_dispatch(
        provider,
        route,
        prompt,
        *,
        operation_id,
        expected_runtime_generation=None,
    ):
        dispatches.append(operation_id)
        return PromptReceipt(operation_id=operation_id, status="streaming")

    monkeypatch.setattr(
        InMemoryHermesProvider, "history_readonly", missing_existing_history
    )
    monkeypatch.setattr(InMemoryHermesProvider, "submit_prompt", record_dispatch)

    operation_id = "blocked-existing-404"
    submitted = client.post(
        f"/api/v1/sessions/{session['id']}/prompts",
        headers=mutation_headers(csrf, operation_id),
        json={"content": "This must never reach Hermes"},
    )
    assert submitted.status_code == 409, submitted.text
    assert dispatches == []

    with app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        operation = db.scalar(
            select(IdempotencyOperation).where(
                IdempotencyOperation.user_id == row.owner_id,
                IdempotencyOperation.scope == f"session:{row.id}:prompt",
                IdempotencyOperation.idempotency_key == operation_id,
            )
        )
        assert row.initial_history_pending is False
        assert operation is not None
        assert operation.status == "failed"


def test_prompt_operation_is_queryable_and_closed_by_terminal_event(authenticated):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "operation-session")
    operation_id = "durable-operation"
    submitted = client.post(
        f"/api/v1/sessions/{session['id']}/prompts",
        headers=mutation_headers(csrf, operation_id),
        json={"content": "Track this operation"},
    )
    assert submitted.status_code == 202, submitted.text
    status = client.get(
        f"/api/v1/sessions/{session['id']}/operations/{operation_id}"
    )
    assert status.status_code == 200
    # The deterministic provider may finish before the status read.  Durable
    # persistence must make either an in-flight or terminal state observable.
    assert status.json()["status"] in {"accepted", "queued", "streaming", "completed"}

    with client.app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        actor_id = row.owner_id
        runtime_generation = row.runtime_generation
    bound = bind_owned_realtime_event(
        client.app.state.session_factory,
        user_id=actor_id,
        payload={
            "type": "message.complete",
            "gatewayId": session["gatewayId"],
            "profileName": "control-dev",
            "storedSessionId": session["storedSessionId"],
            "runtimeSessionId": session["runtimeSessionId"],
            "_runtimeGeneration": runtime_generation,
            "correlationId": operation_id,
            "seq": 9,
            "replayEpoch": "operation-epoch",
        },
    )
    assert bound and bound["controlSessionId"] == session["id"]
    completed = client.get(
        f"/api/v1/sessions/{session['id']}/operations/{operation_id}"
    )
    assert completed.json()["status"] == "completed"
    with client.app.state.session_factory() as db:
        assert db.get(SessionLink, session["id"]).status == "ready"


def test_history_exposes_active_operation_for_pwa_process_recovery(authenticated):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "pwa-recovery-session")
    operation_id = "pwa-recovery-operation"
    accepted_at = "2026-08-30T20:15:00+00:00"

    with client.app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        row.status = "streaming"
        db.add(
            IdempotencyOperation(
                user_id=row.owner_id,
                scope=f"session:{row.id}:prompt",
                idempotency_key=operation_id,
                status="streaming",
                response_json={
                    "operationId": operation_id,
                    "status": "streaming",
                    "acceptedAt": accepted_at,
                },
            )
        )
        db.commit()

    active = client.get(f"/api/v1/sessions/{session['id']}/messages")
    assert active.status_code == 200, active.text
    assert active.json() == {
        "items": [],
        "sessionStatus": "streaming",
        "activeOperation": {
            "operationId": operation_id,
            "status": "streaming",
            "acceptedAt": accepted_at,
        },
    }

    with client.app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        operation = db.scalar(
            select(IdempotencyOperation).where(
                IdempotencyOperation.user_id == row.owner_id,
                IdempotencyOperation.scope == f"session:{row.id}:prompt",
                IdempotencyOperation.idempotency_key == operation_id,
            )
        )
        operation.status = "completed"
        row.status = "ready"
        db.commit()

    settled = client.get(f"/api/v1/sessions/{session['id']}/messages")
    assert settled.status_code == 200, settled.text
    assert settled.json()["sessionStatus"] == "ready"
    assert settled.json()["activeOperation"] is None


def test_second_prompt_is_rejected_while_the_session_has_an_unresolved_operation(
    authenticated,
):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "single-active-session")
    with client.app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        db.add(
            IdempotencyOperation(
                user_id=row.owner_id,
                scope=f"session:{row.id}:prompt",
                idempotency_key="single-active-first",
                status="streaming",
                response_json={
                    "operationId": "single-active-first",
                    "status": "streaming",
                },
            )
        )
        db.commit()

    second = client.post(
        f"/api/v1/sessions/{session['id']}/prompts",
        headers=mutation_headers(csrf, "single-active-second"),
        json={"content": "This must not overlap the first prompt"},
    )
    assert second.status_code == 409
    assert "unresolved prompt" in second.text


def test_history_reconciles_a_completed_prompt_when_the_terminal_event_was_missed(
    authenticated, monkeypatch
):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "history-reconcile-session")

    async def complete_without_event(
        provider,
        route,
        prompt,
        *,
        operation_id,
        expected_runtime_generation=None,
    ):
        provider._messages[route.stored_session_id].extend(
            [
                {"id": "missed-user", "role": "user", "content": prompt},
                {
                    "id": "missed-assistant",
                    "role": "assistant",
                    "content": "Completed while the event stream was unavailable",
                },
            ]
        )
        return PromptReceipt(operation_id=operation_id, status="streaming")

    monkeypatch.setattr(InMemoryHermesProvider, "submit_prompt", complete_without_event)
    operation_id = "history-reconcile-operation"
    submitted = client.post(
        f"/api/v1/sessions/{session['id']}/prompts",
        headers=mutation_headers(csrf, operation_id),
        json={"content": "Recover me from durable history"},
    )
    assert submitted.status_code == 202, submitted.text
    assert submitted.json()["status"] == "streaming"

    history = client.get(f"/api/v1/sessions/{session['id']}/messages")
    assert history.status_code == 200
    status = client.get(
        f"/api/v1/sessions/{session['id']}/operations/{operation_id}"
    )
    assert status.status_code == 200
    assert status.json()["status"] == "completed"


def test_history_does_not_guess_completion_from_only_an_accepted_user_message(
    authenticated, app
):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "history-ambiguous-session")

    async def fail_after_accept():
        with app.state.session_factory() as db:
            row = db.get(SessionLink, session["id"])
            from hermes_control_api.services import GatewayService

            connection = await GatewayService(app.state.services).connection(
                db, row.gateway_id, row.profile_name
            )
        provider = await app.state.services.provider_pool.get(connection)
        provider.fail_next_prompt_after_accept = True

    client.portal.call(fail_after_accept)
    operation_id = "history-stays-ambiguous"
    submitted = client.post(
        f"/api/v1/sessions/{session['id']}/prompts",
        headers=mutation_headers(csrf, operation_id),
        json={"content": "Accepted without a confirmed answer"},
    )
    assert submitted.status_code == 409

    history = client.get(f"/api/v1/sessions/{session['id']}/messages")
    assert history.status_code == 200
    status = client.get(
        f"/api/v1/sessions/{session['id']}/operations/{operation_id}"
    )
    assert status.status_code == 200
    assert status.json()["status"] == "delivery_unknown"


def test_archive_is_normal_and_upstream_delete_requires_exact_confirmation(authenticated):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "deletable-session")
    archived = client.patch(
        f"/api/v1/sessions/{session['id']}",
        headers=mutation_headers(csrf, "archive-session"),
        json={"archived": True},
    )
    assert archived.status_code == 200
    assert archived.json()["archivedAt"] is not None

    rejected = client.delete(
        f"/api/v1/sessions/{session['id']}",
        headers=mutation_headers(csrf, "delete-session-bad"),
    )
    assert rejected.status_code == 409
    deleted = client.delete(
        f"/api/v1/sessions/{session['id']}",
        headers={
            **mutation_headers(csrf, "delete-session-good"),
            "X-Confirm-Delete": session["storedSessionId"],
        },
    )
    assert deleted.status_code == 204, deleted.text


def test_session_title_cannot_be_overridden_only_in_control(authenticated):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "Hermes owns this title")

    rejected = client.patch(
        f"/api/v1/sessions/{session['id']}",
        headers=mutation_headers(csrf, "reject-local-title"),
        json={"title": "Control-only title"},
    )

    assert rejected.status_code == 422
    refreshed = client.get("/api/v1/bootstrap")
    current = next(
        item for item in refreshed.json()["sessions"] if item["id"] == session["id"]
    )
    assert current["title"] == session["title"]
    assert current["title"] != "Control-only title"


def test_session_can_have_a_control_only_display_title(authenticated, app):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "Hermes owns this title")

    renamed = client.patch(
        f"/api/v1/sessions/{session['id']}",
        headers=mutation_headers(csrf, "rename-local-display-title"),
        json={"displayTitle": "  Proyecto   Turing  "},
    )

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "Proyecto Turing"
    with app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        assert row.title == session["title"]
        assert row.display_title == "Proyecto Turing"

    refreshed = client.get("/api/v1/bootstrap")
    current = next(
        item for item in refreshed.json()["sessions"] if item["id"] == session["id"]
    )
    assert current["title"] == "Proyecto Turing"


def test_session_display_title_rejects_blank_and_oversized_values(authenticated):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "display-title-validation")

    blank = client.patch(
        f"/api/v1/sessions/{session['id']}",
        headers=mutation_headers(csrf, "rename-local-blank"),
        json={"displayTitle": "   "},
    )
    oversized = client.patch(
        f"/api/v1/sessions/{session['id']}",
        headers=mutation_headers(csrf, "rename-local-oversized"),
        json={"displayTitle": "x" * 301},
    )

    assert blank.status_code == 422
    assert oversized.status_code == 422


def test_sync_archives_only_links_missing_from_complete_hermes_inventory(
    authenticated, app
):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "missing-upstream-session")
    persisted = client.post(
        f"/api/v1/sessions/{session['id']}/prompts",
        headers=mutation_headers(csrf, "persist-before-external-delete"),
        json={"content": "Create the durable Hermes session first"},
    )
    assert persisted.status_code == 202, persisted.text

    async def delete_outside_control():
        with app.state.session_factory() as db:
            row = db.get(SessionLink, session["id"])
            from hermes_control_api.services import GatewayService

            connection = await GatewayService(app.state.services).connection(
                db, row.gateway_id, row.profile_name
            )
        provider = await app.state.services.provider_pool.get(connection)
        provider._sessions.pop(row.stored_session_id, None)
        provider._messages.pop(row.stored_session_id, None)

    client.portal.call(delete_outside_control)
    synced = client.post(
        "/api/v1/sessions/sync",
        headers=mutation_headers(csrf, "sync-after-external-delete"),
        json={
            "gatewayId": session["gatewayId"],
            "profileName": "control-dev",
        },
    )
    assert synced.status_code == 200, synced.text
    with app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        assert row.status == "missing"
        assert row.archived_at is not None
        assert row.runtime_session_id is None
    assert session["id"] not in {
        item["id"] for item in client.get("/api/v1/bootstrap").json()["sessions"]
    }


def test_session_export_is_hermes_backed_bounded_and_sanitized(authenticated, app):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "export-session")

    async def seed_export_history():
        with app.state.session_factory() as db:
            row = db.get(SessionLink, session["id"])
            from hermes_control_api.services import GatewayService

            connection = await GatewayService(app.state.services).connection(
                db, row.gateway_id, row.profile_name
            )
        provider = await app.state.services.provider_pool.get(connection)
        provider._messages[row.stored_session_id] = [
            {
                "id": "safe-message",
                "role": "assistant",
                "content": "Resultado visible",
                "reasoning": "internal chain of thought",
            },
            {
                "id": "secret-shape",
                "role": "user",
                "content": "token=sk-proj-abcdefghijklmnop",
            },
        ]

    client.portal.call(seed_export_history)
    response = client.get(f"/api/v1/sessions/{session['id']}/export")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith("attachment;")
    body = response.json()
    assert body["sourceOfTruth"] == "Hermes"
    assert body["session"]["storedSessionId"] == session["storedSessionId"]
    rendered = response.text.lower()
    assert "internal chain of thought" not in rendered
    assert "sk-proj-abcdefghijklmnop" not in rendered
    assert "resultado visible" in rendered


def test_session_voice_note_is_history_bound_path_safe_and_range_capable(
    authenticated, app, tmp_path
):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "voice-note-session")
    profiles_root = tmp_path / "profiles"
    audio_root = profiles_root / "control-dev" / "cache" / "audio"
    audio_root.mkdir(parents=True)
    voice_note = audio_root / "tts_test.mp3"
    voice_bytes = b"ID3" + bytes(range(64))
    voice_note.write_bytes(voice_bytes)
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"must-never-be-served")
    escaped_link = audio_root / "escaped.mp3"
    escaped_link.symlink_to(outside)
    app.state.services.settings.hermes_media_root = str(profiles_root)

    async def seed_voice_history():
        with app.state.session_factory() as db:
            row = db.get(SessionLink, session["id"])
            from hermes_control_api.services import GatewayService

            connection = await GatewayService(app.state.services).connection(
                db, row.gateway_id, row.profile_name
            )
        provider = await app.state.services.provider_pool.get(connection)
        provider._messages[row.stored_session_id] = [
            {
                "id": "voice-message",
                "role": "assistant",
                "content": f"Nota de voz\nMEDIA:{voice_note}",
            },
            {
                "id": "escaped-message",
                "role": "assistant",
                "content": f"No reproducir\nMEDIA:{escaped_link}",
            },
        ]

    client.portal.call(seed_voice_history)
    history = client.get(f"/api/v1/sessions/{session['id']}/messages")
    assert history.status_code == 200, history.text
    items = history.json()["items"]
    voice = items[0]
    escaped = items[1]
    assert voice["content"] == "Nota de voz"
    assert str(voice_note) not in history.text
    assert voice["controlMedia"] == [
        {
            "id": voice["controlMedia"][0]["id"],
            "kind": "audio",
            "mediaType": "audio/mpeg",
        }
    ]
    media_id = voice["controlMedia"][0]["id"]
    assert len(media_id) == 32
    assert "controlMedia" not in escaped

    response = client.get(
        f"/api/v1/sessions/{session['id']}/media/{media_id}"
    )
    assert response.status_code == 200, response.text
    assert response.content == voice_bytes
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == 'inline; filename="voice-note.mp3"'
    assert response.headers["x-content-type-options"] == "nosniff"

    ranged = client.get(
        f"/api/v1/sessions/{session['id']}/media/{media_id}",
        headers={"Range": "bytes=0-3"},
    )
    assert ranged.status_code == 206
    assert ranged.content == voice_bytes[:4]
    assert ranged.headers["content-range"] == f"bytes 0-3/{len(voice_bytes)}"

    missing = client.get(
        f"/api/v1/sessions/{session['id']}/media/{'0' * 32}"
    )
    assert missing.status_code == 404


def test_realtime_binding_requires_all_ids_to_match_and_resets_epoch(authenticated):
    client, csrf = authenticated
    first = create_session(client, csrf, "control-dev", "route-first")
    second = create_session(client, csrf, "control-dev", "route-second")
    with client.app.state.session_factory() as db:
        first_row = db.get(SessionLink, first["id"])
        second_row = db.get(SessionLink, second["id"])
        actor_id = first_row.owner_id
        first_generation = first_row.runtime_generation
        second_generation = second_row.runtime_generation

    mismatched = bind_owned_realtime_event(
        client.app.state.session_factory,
        user_id=actor_id,
        payload={
            "type": "message.delta",
            "gatewayId": first["gatewayId"],
            "profileName": "control-dev",
            "storedSessionId": first["storedSessionId"],
            "runtimeSessionId": second["runtimeSessionId"],
            "_runtimeGeneration": second_generation,
            "seq": 1,
            "replayEpoch": "epoch-a",
        },
    )
    assert mismatched is None

    with client.app.state.session_factory() as db:
        row = db.get(SessionLink, first["id"])
        row.last_sequence = 100
        row.replay_epoch = "epoch-old"
        db.commit()
    bound = bind_owned_realtime_event(
        client.app.state.session_factory,
        user_id=actor_id,
        payload={
            "type": "control.reconcile",
            "gatewayId": first["gatewayId"],
            "profileName": "control-dev",
            "storedSessionId": first["storedSessionId"],
            "runtimeSessionId": first["runtimeSessionId"],
            "_runtimeGeneration": first_generation,
            "replayEpoch": "epoch-new",
            "reconciliationRequired": True,
        },
    )
    assert bound and bound["controlSessionId"] == first["id"]
    with client.app.state.session_factory() as db:
        row = db.get(SessionLink, first["id"])
        assert row.last_sequence == 0
        assert row.replay_epoch == "epoch-new"
