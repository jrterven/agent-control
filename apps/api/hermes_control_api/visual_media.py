"""Private immutable image publication, independent of connector availability.

All public reads are authorized by the current session route. Ingestion accepts
the authenticated Connector object, never a model-supplied owner. Synchronous
methods doing object I/O should run in a worker thread with their own DB session.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import re
import warnings
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .config import Settings
from .connector_models import Connector
from .models import Gateway, SessionLink, User, VisualMedia, VisualMediaRouteTombstone, utc_now

MEDIA_ID = re.compile(r"^[0-9a-f]{32}$")
IMAGE_REFERENCE = re.compile(r"!\[((?:\\.|[^\]\\])*)\]\(ac-media:([0-9a-f]{32})\)")
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class MediaValidationError(ValueError):
    pass


class BlobStore(Protocol):
    def put(self, key: str, content: bytes, media_type: str) -> None: ...
    def get(self, key: str, maximum: int) -> bytes: ...
    def delete(self, key: str) -> None: ...


class S3BlobStore:
    def __init__(self, settings: Settings):
        if not all((settings.visual_media_endpoint_url, settings.visual_media_bucket,
                    settings.visual_media_access_key_id, settings.visual_media_secret_access_key)):
            raise MediaValidationError("storage_unavailable")
        endpoint = urlsplit(settings.visual_media_endpoint_url)
        if endpoint.scheme != "https" or endpoint.username or endpoint.password or not endpoint.hostname:
            raise MediaValidationError("storage_unavailable")
        import boto3
        from botocore.config import Config
        self.bucket = settings.visual_media_bucket
        self.client = boto3.client(
            "s3", endpoint_url=settings.visual_media_endpoint_url,
            aws_access_key_id=settings.visual_media_access_key_id,
            aws_secret_access_key=settings.visual_media_secret_access_key,
            region_name=settings.visual_media_region,
            config=Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2}),
        )

    def put(self, key: str, content: bytes, media_type: str) -> None:
        # Content-addressed keys are deterministic across retries. Only this
        # private service has credentials; there are no public/signed write URLs.
        from botocore.exceptions import ClientError
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=content,
                                   ContentType=media_type, CacheControl="private, no-store", IfNoneMatch="*")
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") not in (409, 412):
                raise
            if self.get(key, len(content)) != content:
                raise MediaValidationError("immutable_conflict") from None

    def get(self, key: str, maximum: int) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        stream = response["Body"]
        try:
            if response.get("ContentLength", maximum + 1) > maximum:
                raise MediaValidationError("integrity_failed")
            content = stream.read(maximum + 1)
            if len(content) > maximum:
                raise MediaValidationError("integrity_failed")
            return content
        finally:
            stream.close()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def source_url(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise MediaValidationError("invalid_metadata")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                or parsed.port not in (None, 443) or "\\" in value
                or host.lower().rstrip(".").endswith((".localhost", ".local", ".internal"))
                or host.lower().rstrip(".") == "localhost"):
            raise MediaValidationError("invalid_metadata")
        try:
            if not ipaddress.ip_address(host).is_global:
                raise MediaValidationError("invalid_metadata")
        except ValueError as exc:
            if isinstance(exc, MediaValidationError):
                raise
    except (ValueError, UnicodeError):
        raise MediaValidationError("invalid_metadata") from None
    return value


def _text(metadata: dict, field: str, limit: int, required: bool = False) -> str | None:
    value = metadata.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise MediaValidationError("invalid_metadata")
    if required and not value.strip():
        raise MediaValidationError("invalid_metadata")
    return value


def normalize_image(content: bytes, settings: Settings) -> tuple[bytes, bytes, str, int, int]:
    """Decode and reencode pixels, removing EXIF/XMP/ICC and active payloads."""
    if not isinstance(content, bytes) or not 0 < len(content) <= settings.visual_media_max_bytes:
        raise MediaValidationError("image_too_large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as original:
                fmt = original.format
                if (fmt not in FORMATS or getattr(original, "n_frames", 1) != 1
                        or original.width * original.height > settings.visual_media_max_pixels):
                    raise MediaValidationError("unsupported_image")
                original.load()
                oriented = ImageOps.exif_transpose(original)
                mode = "RGBA" if "A" in oriented.getbands() and fmt != "JPEG" else "RGB"
                # A brand-new pixel image carries no metadata from the decoder.
                pixels = Image.frombytes(mode, oriented.size, oriented.convert(mode).tobytes())
                full = io.BytesIO()
                options = {"quality": 90} if fmt in ("JPEG", "WEBP") else {}
                pixels.save(full, format=fmt, **options)
                width, height = pixels.size
                pixels.thumbnail((768, 768), Image.Resampling.LANCZOS)
                thumbnail = io.BytesIO()
                pixels.save(thumbnail, format="WEBP", quality=82)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        if isinstance(exc, MediaValidationError):
            raise
        raise MediaValidationError("unsupported_image") from None
    if len(full.getvalue()) > settings.visual_media_max_bytes:
        raise MediaValidationError("image_too_large")
    return full.getvalue(), thumbnail.getvalue(), FORMATS[fmt], width, height


def _route_filter(row):
    return (VisualMedia.owner_id == row.owner_id, VisualMedia.gateway_id == row.gateway_id,
            VisualMedia.profile_name == row.profile_name, VisualMedia.stored_session_id == row.stored_session_id)


def _route_tuple(row):
    return row.owner_id, row.gateway_id, row.profile_name, row.stored_session_id


def _key(row: VisualMedia, variant: str) -> str:
    digest = row.thumbnail_hash if variant == "thumbnail" else row.content_hash
    return f"images/{row.owner_id}/{row.id}/{variant}-{digest}"


def public_metadata(row: VisualMedia) -> dict[str, Any]:
    result = dict(id=row.id, kind="image", status=row.status, mediaType=row.media_type,
                  width=row.width, height=row.height, alt=row.alt, provenance=row.provenance)
    for field, value in (("caption", row.caption), ("sourceUrl", row.source_url),
                         ("sourceTitle", row.source_title), ("errorCode", row.error_code)):
        if value is not None:
            result[field] = value
    return result


def image_references(content: str, limit: int = 24) -> list[str]:
    # A metadata item per ID; Markdown preserves repeated placement and ordering.
    return list(dict.fromkeys(match.group(2) for match in IMAGE_REFERENCE.finditer(content)))[:limit]


class VisualMediaService:
    def __init__(self, settings: Settings, store: BlobStore | None = None):
        self.settings = settings
        self._store = store

    @property
    def configured(self) -> bool:
        return self._store is not None or all((self.settings.visual_media_endpoint_url,
            self.settings.visual_media_bucket, self.settings.visual_media_access_key_id,
            self.settings.visual_media_secret_access_key))

    @property
    def store(self) -> BlobStore:
        if self._store is None:
            self._store = S3BlobStore(self.settings)
        return self._store

    @staticmethod
    def _connector_authorized(db: Session, connector: Connector, profile_name: str) -> bool:
        current = db.get(Connector, connector.id, populate_existing=True)
        gateway = db.get(Gateway, connector.gateway_id, populate_existing=True)
        return bool(current and current.owner_id == connector.owner_id
                    and current.gateway_id == connector.gateway_id and not current.revoked_at
                    and profile_name in current.profiles and gateway and gateway.enabled
                    and gateway.owner_id == connector.owner_id and gateway.transport_kind == "connector")

    def ingest(self, db: Session, connector: Connector, *, media_id: str, profile_name: str,
               stored_session_id: str, metadata: dict, content: bytes,
               thumbnail: bytes | None = None) -> dict[str, Any]:
        """Idempotent publication; safe errors disclose neither routes nor storage."""
        failure = lambda code: {"id": media_id, "status": "failed", "errorCode": code}
        if (not isinstance(media_id, str) or not MEDIA_ID.fullmatch(media_id)
                or not isinstance(profile_name, str) or not 0 < len(profile_name) <= 120
                or not isinstance(stored_session_id, str) or not 0 < len(stored_session_id) <= 255
                or any(ord(c) < 32 for c in stored_session_id)):
            return failure("invalid_metadata")
        if not self._connector_authorized(db, connector, profile_name):
            return failure("route_unavailable")
        route = (connector.owner_id, connector.gateway_id, profile_name, stored_session_id)
        if db.get(VisualMediaRouteTombstone, route):
            return failure("route_unavailable")
        try:
            if not isinstance(metadata, dict) or metadata.get("provenance") not in {"web", "generated", "local"}:
                raise MediaValidationError("invalid_metadata")
            meta = dict(alt=_text(metadata, "alt", 1000, required=True),
                        caption=_text(metadata, "caption", 2000), source_url=source_url(metadata.get("sourceUrl")),
                        source_title=_text(metadata, "sourceTitle", 300), provenance=metadata["provenance"])
            if meta["provenance"] == "web" and not meta["source_url"]:
                raise MediaValidationError("invalid_metadata")
            full, thumb, mime, width, height = normalize_image(content, self.settings)
            if (metadata.get("mediaType") != mime or metadata.get("width") != width
                    or metadata.get("height") != height):
                raise MediaValidationError("invalid_metadata")
            if thumbnail is not None:
                # Never trust connector thumbnails. Validate supplied bytes, then
                # serve the thumbnail derived from the verified full image.
                if len(thumbnail) > 1024 * 1024:
                    raise MediaValidationError("unsupported_image")
                normalize_image(thumbnail, self.settings)
            request_hash = hashlib.sha256(content + b"\0" + (thumbnail or b"") + b"\0"
                                          + json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        except (MediaValidationError, TypeError) as exc:
            return failure(str(exc) if isinstance(exc, MediaValidationError) else "invalid_metadata")
        # Serialize reservations for a user across processes, not only this WS.
        owner = db.scalar(select(User).where(User.id == connector.owner_id, User.is_active.is_(True)).with_for_update())
        if owner is None:
            db.rollback()
            return failure("route_unavailable")
        row = db.get(VisualMedia, media_id, populate_existing=True)
        if row:
            if (_route_tuple(row) != route or row.deleted_at or row.purged_at
                    or row.request_hash != request_hash):
                db.rollback()
                return failure("immutable_conflict")
            if row.status == "ready":
                db.commit()
                return {"id": media_id, "status": "ready"}
            if (row.content_hash != hashlib.sha256(full).hexdigest()
                    or row.thumbnail_hash != hashlib.sha256(thumb).hexdigest()):
                # Encoder upgrades must never write different bytes under a
                # previous reservation's content-addressed immutable keys.
                db.rollback()
                return failure("immutable_conflict")
        else:
            used = db.scalar(select(func.coalesce(func.sum(VisualMedia.byte_size + VisualMedia.thumbnail_byte_size), 0))
                             .where(VisualMedia.owner_id == connector.owner_id, VisualMedia.purged_at.is_(None))) or 0
            if used + len(full) + len(thumb) > self.settings.visual_media_quota_bytes:
                db.rollback()
                return failure("quota_exceeded")
            row = VisualMedia(id=media_id, owner_id=route[0], gateway_id=route[1], profile_name=route[2],
                              stored_session_id=route[3], status="pending", request_hash=request_hash,
                              content_hash=hashlib.sha256(full).hexdigest(), thumbnail_hash=hashlib.sha256(thumb).hexdigest(),
                              byte_size=len(full), thumbnail_byte_size=len(thumb), media_type=mime,
                              width=width, height=height, **meta)
            db.add(row)
        # Persist reservation before network I/O, including object keys needed
        # for cleanup when a process dies between the two uploads.
        db.commit()
        try:
            self.store.put(_key(row, "full"), full, mime)
            self.store.put(_key(row, "thumbnail"), thumb, "image/webp")
        except Exception:
            row.error_code = "storage_unavailable"
            db.commit()
            return failure("storage_unavailable")
        db.refresh(row)
        # Recheck revocation/deletion after I/O; a delete wins against inflight publication.
        if row.deleted_at or db.get(VisualMediaRouteTombstone, route) or not self._connector_authorized(db, connector, profile_name):
            row.deleted_at = row.deleted_at or utc_now()
            db.commit()
            return failure("route_unavailable")
        row.status, row.error_code = "ready", None
        db.commit()
        return {"id": media_id, "status": "ready"}

    def authorized(self, db: Session, actor: User, session: SessionLink, media_id: str) -> VisualMedia | None:
        if not MEDIA_ID.fullmatch(media_id) or session.owner_id != actor.id:
            return None
        connector = db.scalar(select(Connector).where(Connector.gateway_id == session.gateway_id,
                                                      Connector.owner_id == actor.id))
        if not connector or not self._connector_authorized(db, connector, session.profile_name):
            return None
        return db.scalar(select(VisualMedia).where(VisualMedia.id == media_id, *_route_filter(session),
                                                   VisualMedia.deleted_at.is_(None), VisualMedia.purged_at.is_(None)))

    def content(self, row: VisualMedia, variant: str = "full") -> tuple[bytes, str]:
        if variant not in {"thumbnail", "full"} or row.status != "ready" or row.deleted_at:
            raise MediaValidationError("media_unavailable")
        maximum = row.thumbnail_byte_size if variant == "thumbnail" else row.byte_size
        expected = row.thumbnail_hash if variant == "thumbnail" else row.content_hash
        content = self.store.get(_key(row, variant), maximum)
        if len(content) != maximum or hashlib.sha256(content).hexdigest() != expected:
            raise MediaValidationError("integrity_failed")
        return content, "image/webp" if variant == "thumbnail" else row.media_type

    def project(self, db: Session, session: SessionLink, content: str) -> list[dict]:
        ids = image_references(content)
        if not ids:
            return []
        connector = db.scalar(select(Connector).where(Connector.gateway_id == session.gateway_id,
                                                      Connector.owner_id == session.owner_id))
        if not connector or not self._connector_authorized(db, connector, session.profile_name):
            return []
        records = {row.id: row for row in db.scalars(select(VisualMedia).where(
            VisualMedia.id.in_(ids), *_route_filter(session), VisualMedia.deleted_at.is_(None),
            VisualMedia.purged_at.is_(None)))}
        return [public_metadata(records[id_]) for id_ in ids if id_ in records]

    def garbage_collect(self, db: Session) -> int:
        cutoff = utc_now() - timedelta(days=self.settings.visual_media_retention_days)
        rows = list(db.scalars(select(VisualMedia).where(VisualMedia.purged_at.is_(None), or_(
            VisualMedia.deleted_at <= cutoff,
            (VisualMedia.status == "pending") & (VisualMedia.created_at <= cutoff),
            (VisualMedia.owner_id.not_in(select(User.id))) & (VisualMedia.created_at <= cutoff),
        ))))
        for row in rows:
            self.store.delete(_key(row, "full"))
            self.store.delete(_key(row, "thumbnail"))
            row.purged_at = utc_now()
            row.deleted_at = row.deleted_at or row.purged_at
            db.commit()
        return len(rows)

    def backup(self, db: Session, directory: Path) -> dict:
        """Pair this manifest/blob snapshot with the database backup."""
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        entries = []
        for row in db.scalars(select(VisualMedia).where(VisualMedia.status == "ready", VisualMedia.purged_at.is_(None))):
            entry = dict(id=row.id, ownerId=row.owner_id, gatewayId=row.gateway_id,
                         profileName=row.profile_name, storedSessionId=row.stored_session_id, objects=[])
            for variant in ("full", "thumbnail"):
                size = row.thumbnail_byte_size if variant == "thumbnail" else row.byte_size
                digest = row.thumbnail_hash if variant == "thumbnail" else row.content_hash
                data = self.store.get(_key(row, variant), size)
                if hashlib.sha256(data).hexdigest() != digest or len(data) != size:
                    raise MediaValidationError("integrity_failed")
                name = f"{row.id}-{variant}-{digest}"
                (directory / name).write_bytes(data)
                entry["objects"].append(dict(variant=variant, file=name, sha256=digest, size=size))
            entries.append(entry)
        manifest = {"version": 1, "createdAt": utc_now().isoformat(), "assets": entries}
        (directory / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        return manifest

    def restore(self, db: Session, directory: Path) -> int:
        """Restore only blobs that match the already-restored authoritative DB."""
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("version") != 1:
            raise MediaValidationError("invalid_backup")
        verified = []
        seen = set()
        for entry in manifest["assets"]:
            row = db.get(VisualMedia, entry["id"])
            if (not row or row.purged_at or row.status != "ready" or row.id in seen
                    or _route_tuple(row) != (entry["ownerId"], entry["gatewayId"], entry["profileName"], entry["storedSessionId"])):
                raise MediaValidationError("invalid_backup")
            seen.add(row.id)
            if {obj["variant"] for obj in entry["objects"]} != {"full", "thumbnail"} or len(entry["objects"]) != 2:
                raise MediaValidationError("invalid_backup")
            for obj in entry["objects"]:
                variant = obj["variant"]
                digest = row.thumbnail_hash if variant == "thumbnail" else row.content_hash
                size = row.thumbnail_byte_size if variant == "thumbnail" else row.byte_size
                if obj["file"] != f"{row.id}-{variant}-{digest}" or obj["sha256"] != digest or obj["size"] != size:
                    raise MediaValidationError("invalid_backup")
                path = directory / obj["file"]
                if path.is_symlink() or path.stat().st_size != size:
                    raise MediaValidationError("invalid_backup")
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != digest:
                    raise MediaValidationError("integrity_failed")
                verified.append((_key(row, variant), path, digest, size, "image/webp" if variant == "thumbnail" else row.media_type))
        # Completeness prevents a 'successful' restore silently omitting images.
        expected = set(db.scalars(select(VisualMedia.id).where(VisualMedia.status == "ready", VisualMedia.purged_at.is_(None))))
        if seen != expected:
            raise MediaValidationError("invalid_backup")
        for key, path, digest, size, media_type in verified:
            if path.is_symlink() or path.stat().st_size != size:
                raise MediaValidationError("invalid_backup")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise MediaValidationError("integrity_failed")
            self.store.put(key, data, media_type)
            if self.store.get(key, len(data)) != data:
                raise MediaValidationError("integrity_failed")
        return len(seen)

    def verify_backup(self, db: Session, directory: Path) -> int:
        """Exercise restore writes/reads in an isolated temporary store, never R2."""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="agent-control-media-restore-", dir=directory.parent) as temporary:
            class RehearsalStore:
                def path(self, key):
                    return Path(temporary) / hashlib.sha256(key.encode()).hexdigest()
                def put(self, key, content, media_type):
                    self.path(key).write_bytes(content)
                def get(self, key, maximum):
                    with self.path(key).open("rb") as stream:
                        return stream.read(maximum + 1)
                def delete(self, key):
                    self.path(key).unlink(missing_ok=True)
            return VisualMediaService(self.settings, RehearsalStore()).restore(db, directory)


def get_visual_media_service(services) -> VisualMediaService:
    if services.visual_media is None:
        services.visual_media = VisualMediaService(services.settings)
    return services.visual_media


def main():
    parser = argparse.ArgumentParser(description="Private image backup, restore and retention")
    parser.add_argument("action", choices=("backup", "restore", "verify-backup", "gc"))
    parser.add_argument("directory", type=Path, nargs="?")
    parser.add_argument("--database", help="Isolated restored database for backup/rehearsal")
    args = parser.parse_args()
    if args.action != "gc" and args.directory is None:
        parser.error("backup/restore require a directory")
    from .config import get_settings
    from .database import build_engine, build_session_factory
    settings = get_settings()
    if args.database:
        if not re.fullmatch(r"control_(?:restore|release_check)_[a-z0-9_]{1,50}", args.database):
            parser.error("Only isolated restore/release-check databases are accepted")
        if args.action == "gc":
            parser.error("Garbage collection cannot use a restored database")
        from .cloud_migrations import restored_database_url
        settings = settings.model_copy(update={"database_url": restored_database_url(
            settings.database_url, args.database).render_as_string(hide_password=False)})
    service = VisualMediaService(settings)
    with build_session_factory(build_engine(settings))() as db:
        if args.action == "backup":
            result = {"assets": len(service.backup(db, args.directory)["assets"])}
        elif args.action == "restore":
            result = {"assets": service.restore(db, args.directory)}
        elif args.action == "verify-backup":
            result = {"assets": service.verify_backup(db, args.directory)}
        else:
            result = {"purged": service.garbage_collect(db)}
    print(json.dumps({"status": "verified", **result}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # SDK/DB exceptions may embed secret endpoints or credentials. Operator
        # failures are deliberately bounded and do not print exception traces.
        raise SystemExit("Media operation failed: storage, database or backup verification unavailable") from None
