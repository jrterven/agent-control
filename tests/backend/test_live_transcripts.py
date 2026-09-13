from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from hermes_control_api.models import AuditEvent, IdempotencyOperation, LiveTranscript, ProfileRef, SessionLink, User


@pytest.fixture
def conversation(app):
    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.username == "admin"))
        profile = db.scalar(select(ProfileRef).where(ProfileRef.profile_name == "default"))
        row = SessionLink(owner_id=owner.id, gateway_id=profile.gateway_id, profile_name=profile.profile_name, stored_session_id="voice-history")
        db.add(row)
        db.commit()
        return row.id


def fragment(text=" Mi texto privado", role="user", order=0, start=100):
    return {"role": role, "text": text, "start": start, "end": start + 500, "order": order}


def test_live_transcripts_persist_without_tasks_are_encrypted_and_retry_safe(authenticated, app, conversation):
    client, csrf = authenticated
    path = f"/api/v1/sessions/{conversation}/live-transcripts"
    call_id = str(uuid4())
    headers = {"X-CSRF-Token": csrf}
    first = [fragment()]
    second = first + [fragment(" Te escucho.", "assistant", 1, 300)]
    assert client.put(f"{path}/{call_id}", json={"fragments": first}).status_code == 403
    for fragments in (first, second, second, first):
        response = client.put(f"{path}/{call_id}", headers=headers, json={"fragments": fragments})
        assert response.status_code == 204, response.text
    page = client.get(path)
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert page.json()["items"][0]["fragments"] == second
    assert page.json()["items"][0]["id"] == call_id
    assert page.json()["nextCursor"] is None
    batch = {"offset": 2, "fragments": [fragment(" Otra cosa", order=2, start=1500)]}
    for _ in range(2):
        assert client.put(f"{path}/{call_id}", headers=headers, json=batch).status_code == 204
    assert client.get(path).json()["items"][0]["fragments"] == second + batch["fragments"]
    assert client.put(f"{path}/{call_id}", headers=headers, json={"offset": 5, "fragments": [fragment(order=5)]}).status_code == 409
    assert client.put(f"{path}/{call_id}", headers=headers, json={"fragments": [fragment("replacement")]}).status_code == 409
    with app.state.session_factory() as db:
        row = db.get(LiveTranscript, call_id)
        assert row.revision == 3
        assert row.payload_ciphertext.startswith("v1.")
        assert first[0]["text"] not in row.payload_ciphertext
        for model in (AuditEvent, IdempotencyOperation):
            assert first[0]["text"] not in repr([r.__dict__ for r in db.scalars(select(model))])
        with pytest.raises(ValueError):
            app.state.services.vault.decrypt(row.payload_ciphertext, aad=f"live-transcript:other:{conversation}:{call_id}")
    client.cookies.clear()
    assert client.get(path).status_code == 401


def test_live_transcripts_cannot_cross_owner_session_or_archive(authenticated, app, conversation):
    client, csrf = authenticated
    path = f"/api/v1/sessions/{conversation}/live-transcripts"
    call_id = str(uuid4())
    headers = {"X-CSRF-Token": csrf}
    body = {"fragments": [fragment()]}
    assert client.put(f"{path}/{call_id}", headers=headers, json=body).status_code == 204
    with app.state.session_factory() as db:
        original = db.get(SessionLink, conversation)
        other = User(username="voice-other", password_hash="unused", is_admin=False)
        db.add(other)
        db.flush()
        foreign = SessionLink(owner_id=other.id, gateway_id=original.gateway_id, profile_name=original.profile_name, stored_session_id="foreign-voice")
        second = SessionLink(owner_id=original.owner_id, gateway_id=original.gateway_id, profile_name=original.profile_name, stored_session_id="second-voice")
        db.add_all([foreign, second])
        db.commit()
        foreign_id, second_id = foreign.id, second.id
    for suffix in ("", f"/{call_id}"):
        request = client.get if not suffix else client.put
        kwargs = {} if not suffix else {"headers": headers, "json": body}
        assert request(f"/api/v1/sessions/{foreign_id}/live-transcripts{suffix}", **kwargs).status_code == 404
    assert client.put(f"/api/v1/sessions/{second_id}/live-transcripts/{call_id}", headers=headers, json=body).status_code == 404
    assert client.get(f"/api/v1/sessions/{second_id}/live-transcripts?before={call_id}").status_code == 404
    with app.state.session_factory() as db:
        db.get(SessionLink, conversation).archived_at = datetime.now(timezone.utc)
        db.commit()
    assert client.get(path).status_code == 404
    assert client.put(f"{path}/{call_id}", headers=headers, json=body).status_code == 404


def test_transcript_payload_limits_and_pagination(authenticated, app, conversation):
    client, csrf = authenticated
    path = f"/api/v1/sessions/{conversation}/live-transcripts"
    headers = {"X-CSRF-Token": csrf}
    for fragments in ([], [fragment(role="system")], [fragment(order=2)], [fragment(start=-1)],
                      [{**fragment(), "end": 0}], [fragment("x" * 48_001)],
                      [fragment("😀" * 24_001)]):
        response = client.put(f"{path}/{uuid4()}", headers=headers, json={"fragments": fragments})
        assert response.status_code == 422, response.text
        assert "Mi texto privado" not in response.text
    # A text-bounded call may still exceed the generic body limit because each
    # fragment preserves role, timing and arrival order. Its final snapshot fits.
    large_call = str(uuid4())
    large = [fragment("x", order=i, start=i * 1000) for i in range(20_000)]
    assert client.put(f"{path}/{large_call}", headers=headers, json={"fragments": large}).status_code == 204
    with app.state.session_factory() as db:
        db.delete(db.get(LiveTranscript, large_call))
        db.commit()
    ids = []
    for i in range(12):
        call_id = str(uuid4())
        ids.append(call_id)
        assert client.put(f"{path}/{call_id}", headers=headers, json={"fragments": [fragment(str(i))]}).status_code == 204
    first = client.get(path).json()
    assert [row["id"] for row in first["items"]] == ids[2:]
    second = client.get(path, params={"before": first["nextCursor"]}).json()
    assert [row["id"] for row in second["items"]] == ids[:2]
    assert second["nextCursor"] is None
    with app.state.session_factory() as db:
        db.delete(db.get(SessionLink, conversation))
        db.commit()
        assert db.scalar(select(LiveTranscript)) is None
