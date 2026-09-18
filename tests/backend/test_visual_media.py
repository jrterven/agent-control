from __future__ import annotations

import io
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from PIL import Image, PngImagePlugin
from sqlalchemy import select

from hermes_control_api.connector_models import Connector
from hermes_control_api.models import Gateway, SessionLink, User, VisualMedia, VisualMediaRouteTombstone, utc_now
from hermes_control_api.services import SessionService
from hermes_control_api.visual_media import MediaValidationError, VisualMediaService, normalize_image, source_url


class FakeStore:
    def __init__(self):
        self.objects = {}
        self.puts = 0
        self.fail = False

    def put(self, key, content, media_type):
        if self.fail:
            raise OSError("private storage credentials must not escape")
        if key in self.objects:
            assert self.objects[key] == content
        self.objects[key] = content
        self.puts += 1

    def get(self, key, maximum):
        content = self.objects[key]
        assert len(content) <= maximum
        return content

    def delete(self, key):
        self.objects.pop(key, None)


def picture(color="red", fmt="PNG", size=(80, 60)):
    output = io.BytesIO()
    meta = PngImagePlugin.PngInfo()
    meta.add_text("private", "host path and GPS data")
    Image.new("RGB", size, color).save(output, format=fmt, **({"pnginfo": meta} if fmt == "PNG" else {}))
    return output.getvalue()


@pytest.fixture
def media_env(authenticated, app):
    client, csrf = authenticated
    store = FakeStore()
    service = VisualMediaService(app.state.settings, store)
    app.state.services.visual_media = service
    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.username == "admin"))
        gateway = Gateway(name="image-host", owner_id=owner.id, transport_kind="connector",
                          rest_url="http://unused.invalid", ws_url="ws://unused.invalid")
        db.add(gateway)
        db.flush()
        connector = Connector(owner_id=owner.id, gateway_id=gateway.id, name="Offline image host",
                              profiles=["jarvis", "turing"], token_hash="not-a-token")
        session = SessionLink(owner_id=owner.id, gateway_id=gateway.id, profile_name="jarvis",
                              stored_session_id="hermes-briefing")
        db.add_all([connector, session])
        db.commit()
        yield client, db, service, store, connector, session, owner


def publish(env, **overrides):
    _, db, service, _, connector, session, _ = env
    args = dict(media_id="a" * 32, profile_name=session.profile_name, stored_session_id=session.stored_session_id,
                metadata=dict(alt="Research figure", caption="Architecture", provenance="web",
                              sourceUrl="https://example.org/paper", sourceTitle="Paper",
                              width=80, height=60, mediaType="image/png"), content=picture())
    args.update(overrides)
    return service.ingest(db, connector, **args)


def test_publication_offline_route_and_thumbnail(media_env):
    client, db, service, store, connector, session, owner = media_env
    assert publish(media_env) == {"id": "a" * 32, "status": "ready"}
    assert store.puts == 2
    base = f"/api/v1/sessions/{session.id}/media/{'a' * 32}"
    response = client.get(base + "/metadata")
    assert response.status_code == 200
    assert response.json() == dict(id="a" * 32, kind="image", status="ready", mediaType="image/png",
        width=80, height=60, alt="Research figure", caption="Architecture", sourceUrl="https://example.org/paper",
        sourceTitle="Paper", provenance="web")
    assert "no-store" in response.headers["cache-control"]
    full = client.get(base)
    assert full.status_code == 200 and full.headers["content-type"] == "image/png"
    assert b"host path and GPS data" not in full.content
    thumb = client.get(base + "?variant=thumbnail")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/webp"
    assert client.get(base + "?variant=other").status_code == 422
    assert client.get(base + "/metadata").json()["status"] == "ready"


def test_idempotence_and_immutable_scope(media_env):
    assert publish(media_env)["status"] == "ready"
    assert publish(media_env)["status"] == "ready"
    assert media_env[3].puts == 2
    for update in (dict(content=picture("blue")), dict(profile_name="turing"), dict(stored_session_id="other")):
        assert publish(media_env, **update)["errorCode"] == "immutable_conflict"
    assert media_env[3].puts == 2


def test_unmaterialized_cron_route_can_be_imported_later(media_env):
    client, db, service, store, connector, session, owner = media_env
    assert publish(media_env, stored_session_id="cron-finished-offline")["status"] == "ready"
    assert service.authorized(db, owner, session, "a" * 32) is None
    imported = SessionLink(owner_id=owner.id, gateway_id=connector.gateway_id,
                           profile_name="jarvis", stored_session_id="cron-finished-offline")
    db.add(imported)
    db.commit()
    assert service.authorized(db, owner, imported, "a" * 32) is not None
    assert client.get(f"/api/v1/sessions/{imported.id}/media/{'a' * 32}").status_code == 200


@pytest.mark.parametrize("restriction", ["owner", "profile", "session", "revoked", "unshared", "disabled"])
def test_current_route_and_owner_isolation(media_env, restriction):
    client, db, service, store, connector, session, owner = media_env
    publish(media_env)
    if restriction == "owner":
        owner = User(id="different-owner", username="other", password_hash="none")
    elif restriction == "profile":
        session.profile_name = "turing"
    elif restriction == "session":
        session.stored_session_id = "different-session"
    elif restriction == "revoked":
        connector.revoked_at = utc_now()
    elif restriction == "unshared":
        connector.profiles = ["turing"]
    else:
        db.get(Gateway, connector.gateway_id).enabled = False
    db.commit()
    assert service.authorized(db, owner, session, "a" * 32) is None


def test_unauthenticated_browser_cannot_load(media_env):
    client, _, _, _, _, session, _ = media_env
    publish(media_env)
    client.cookies.clear()
    base = f"/api/v1/sessions/{session.id}/media/{'a' * 32}"
    assert client.get(base).status_code == 401
    assert client.get(base + "/metadata").status_code == 401


def test_quota_reserves_pending_and_retries_storage_failure(media_env):
    _, db, service, store, _, _, _ = media_env
    store.fail = True
    assert publish(media_env)["errorCode"] == "storage_unavailable"
    row = db.get(VisualMedia, "a" * 32)
    assert row.status == "pending" and row.error_code == "storage_unavailable"
    service.settings = service.settings.model_copy(update={"visual_media_quota_bytes": row.byte_size + row.thumbnail_byte_size})
    assert publish(media_env, media_id="b" * 32)["errorCode"] == "quota_exceeded"
    store.fail = False
    assert publish(media_env)["status"] == "ready"
    assert row.error_code is None


@pytest.mark.parametrize("payload", [b"<svg onload='evil()'/>", b"<!doctype html><img>", b"garbage", b""])
def test_invalid_or_active_content_rejected(media_env, payload):
    result = publish(media_env, content=payload)
    assert result["status"] == "failed"
    assert media_env[3].objects == {}


def test_declared_type_dimensions_and_thumbnail_revalidated(media_env):
    meta = dict(alt="Image", provenance="local", width=80, height=60, mediaType="image/png")
    assert publish(media_env, metadata={**meta, "mediaType": "image/jpeg"})["errorCode"] == "invalid_metadata"
    assert publish(media_env, metadata={**meta, "width": 999})["errorCode"] == "invalid_metadata"
    assert publish(media_env, metadata=meta, thumbnail=b"<svg/>")["status"] == "failed"
    assert media_env[3].objects == {}


def test_pixel_limit_and_exif_are_enforced(media_env):
    settings = media_env[2].settings.model_copy(update={"visual_media_max_pixels": 100})
    with pytest.raises(MediaValidationError):
        normalize_image(picture(), settings)
    image = Image.new("RGB", (80, 60))
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "private creator"
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)
    normalized, _, mime, width, height = normalize_image(output.getvalue(), media_env[2].settings)
    assert (width, height) == (60, 80)
    assert not Image.open(io.BytesIO(normalized)).getexif()


@pytest.mark.parametrize("url", ["http://example.com", "https://user:secret@example.com", "https://127.0.0.1/a",
    "https://[::1]/", "https://localhost/", "https://example.local/", "javascript:evil()", "https://example.org:444/a"])
def test_source_urls_are_safe(url):
    with pytest.raises(MediaValidationError):
        source_url(url)


def test_deletion_tombstones_access_and_purges_after_retention(media_env):
    _, db, service, store, connector, session, owner = media_env
    publish(media_env)
    route = (owner.id, session.gateway_id, session.profile_name, session.stored_session_id)
    db.delete(session)
    db.commit()
    row = db.get(VisualMedia, "a" * 32, populate_existing=True)
    assert row.deleted_at and db.get(VisualMediaRouteTombstone, route)
    assert publish(media_env)["errorCode"] == "route_unavailable"
    assert service.garbage_collect(db) == 0 and len(store.objects) == 2
    row.deleted_at = utc_now() - timedelta(days=31)
    db.commit()
    assert service.garbage_collect(db) == 1 and not store.objects
    assert publish(media_env)["errorCode"] == "route_unavailable"


def test_projection_preserves_positions_and_ignores_forged_routes(media_env, app):
    _, db, service, store, connector, session, owner = media_env
    publish(media_env)
    content = f"Before\n\n![Figure](ac-media:{'a' * 32})\n\nAfter\n![Unknown](ac-media:{'b' * 32})"
    result = SessionService(app.state.services)._project_history(db, session, [{"role": "assistant", "content": content}])
    assert result[0]["content"] == content
    assert [item["id"] for item in result[0]["controlMedia"]] == ["a" * 32]
    session.profile_name = "turing"
    assert service.project(db, session, content) == []


def test_backup_restore_and_offline_integrity(media_env, tmp_path):
    _, db, service, store, connector, session, owner = media_env
    publish(media_env)
    manifest = service.backup(db, tmp_path)
    assert len(manifest["assets"]) == 1 and len(manifest["assets"][0]["objects"]) == 2
    assert service.verify_backup(db, tmp_path) == 1
    store.objects.clear()
    assert service.restore(db, tmp_path) == 1
    assert service.content(db.get(VisualMedia, "a" * 32))[1] == "image/png"
    corrupt = tmp_path / manifest["assets"][0]["objects"][0]["file"]
    corrupt.write_bytes(b"x" * corrupt.stat().st_size)
    with pytest.raises(MediaValidationError, match="integrity_failed"):
        service.restore(db, tmp_path)


def test_incomplete_or_traversal_backup_rejected_without_writes(media_env, tmp_path):
    _, db, service, store, _, _, _ = media_env
    publish(media_env)
    manifest = service.backup(db, tmp_path)
    store.puts = 0
    manifest["assets"][0]["objects"][0]["file"] = "../outside"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(MediaValidationError):
        service.restore(db, tmp_path)
    assert store.puts == 0
    (tmp_path / "manifest.json").write_text(json.dumps({"version": 1, "assets": []}))
    with pytest.raises(MediaValidationError):
        service.restore(db, tmp_path)


def test_object_tampering_never_served(media_env):
    _, db, service, store, _, _, _ = media_env
    publish(media_env)
    key = next(key for key in store.objects if "/full-" in key)
    store.objects[key] = b"x" * len(store.objects[key])
    with pytest.raises(MediaValidationError, match="integrity_failed"):
        service.content(db.get(VisualMedia, "a" * 32))


def test_missing_storage_configuration_does_not_fall_back_to_disk(media_env):
    service = VisualMediaService(media_env[2].settings)
    assert not service.configured
    with pytest.raises(MediaValidationError, match="storage_unavailable"):
        service.store


@pytest.mark.parametrize("change", ["delete", "revoke", "unshare"])
def test_route_changes_during_upload_win_over_publication(media_env, change):
    _, db, service, store, connector, session, owner = media_env
    original_put = store.put
    def put(key, content, media_type):
        original_put(key, content, media_type)
        if store.puts == 1:
            if change == "delete":
                db.delete(session)
            elif change == "revoke":
                connector.revoked_at = utc_now()
            else:
                connector.profiles = ["turing"]
            db.commit()
    store.put = put
    assert publish(media_env)["errorCode"] == "route_unavailable"
    row = db.get(VisualMedia, "a" * 32, populate_existing=True)
    assert row.deleted_at and row.status != "ready"
    assert service.authorized(db, owner, session, row.id) is None


def test_same_id_other_account_cannot_overwrite(media_env):
    _, db, service, store, connector, session, owner = media_env
    publish(media_env)
    stranger = User(username="stranger", password_hash="none")
    db.add(stranger)
    db.flush()
    gateway = Gateway(name="second-host", owner_id=stranger.id, transport_kind="connector",
                      rest_url="http://unused.invalid", ws_url="ws://unused.invalid")
    db.add(gateway)
    db.flush()
    second = Connector(owner_id=stranger.id, gateway_id=gateway.id, name="Second", profiles=["jarvis"], token_hash="second")
    db.add(second)
    db.commit()
    meta = dict(alt="Research figure", caption="Architecture", provenance="web",
                sourceUrl="https://example.org/paper", sourceTitle="Paper", width=80, height=60, mediaType="image/png")
    result = service.ingest(db, second, media_id="a" * 32, profile_name="jarvis", stored_session_id=session.stored_session_id,
                           metadata=meta, content=picture())
    assert result["errorCode"] == "immutable_conflict"
    assert store.puts == 2


def test_s3_conditional_write_retries_verify_existing_content():
    from botocore.exceptions import ClientError
    from hermes_control_api.visual_media import S3BlobStore
    class Client:
        content = b"correct"
        def put_object(self, **kwargs):
            assert kwargs["IfNoneMatch"] == "*"
            raise ClientError({"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
        def get_object(self, **kwargs):
            return {"ContentLength": len(self.content), "Body": io.BytesIO(self.content)}
    store = object.__new__(S3BlobStore)
    store.bucket, store.client = "private-product", Client()
    store.put("immutable", b"correct", "image/png")
    store.client.content = b"changed"
    with pytest.raises(MediaValidationError, match="immutable_conflict"):
        store.put("immutable", b"correct", "image/png")
