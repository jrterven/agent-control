"""Private, durable visual publication. Cloud never sees a local source path."""
from __future__ import annotations

import http.client
import ipaddress
import io
import json
from pathlib import Path
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from . import hermes_media_plugin as plugin
from .hermes_media_plugin import MAX_BYTES, load_policy, open_queue, validate_policy
from .tls import cloud_ssl_context

MAX_PIXELS = 25_000_000
RETRYABLE_ERRORS = frozenset({"storage_unavailable", "draining", "session_pending"})
_DNS_SLOTS = threading.BoundedSemaphore(4)


class MediaError(ValueError):
    pass


def public_addresses(host: str, port: int) -> list[str]:
    # getaddrinfo has no portable timeout. Keep at most four bounded, daemon
    # lookups so an unresponsive resolver cannot hold up the durable worker or
    # leak one unbounded thread per URL.
    if not _DNS_SLOTS.acquire(timeout=1):
        raise MediaError("MEDIA_DOWNLOAD_TIMEOUT")
    completed = threading.Event()
    result = []
    def resolve():
        try:
            result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError:
            result.append(None)
        finally:
            _DNS_SLOTS.release()
            completed.set()
    threading.Thread(target=resolve, daemon=True, name="media-dns").start()
    if not completed.wait(5):
        raise MediaError("MEDIA_DOWNLOAD_TIMEOUT")
    try:
        rows = result[0] or []
        addresses = list(dict.fromkeys(item[4][0] for item in rows))
        def safe(address):
            ip = ipaddress.ip_address(address)
            if not ip.is_global or ip.is_multicast or ip.is_reserved:
                return False
            if isinstance(ip, ipaddress.IPv6Address):
                if ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48"):
                    return False
                if ip.sixtofour or ip.teredo:
                    return False
            return True
        if not addresses or any(not safe(ip) for ip in addresses):
            raise MediaError("MEDIA_URL_NOT_PUBLIC")
        return addresses
    except (ValueError, socket.gaierror) as error:
        raise MediaError("MEDIA_URL_NOT_PUBLIC") from error


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS verifies the hostname, but connects only to the validated numeric IP."""
    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, 443, timeout=timeout, context=cloud_ssl_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch_image(url: str, policy: dict | None = None) -> bytes:
    maximum = min(MAX_BYTES, validate_policy(policy)["maxBytes"])
    deadline = time.monotonic() + 20
    for redirect in range(4):
        try:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.port not in {None, 443}:
                raise MediaError("MEDIA_INVALID_URL")
            if len(url) > 2048 or any(ord(c) < 32 for c in url):
                raise MediaError("MEDIA_INVALID_URL")
            host = parsed.hostname.encode("idna").decode("ascii")
            addresses = public_addresses(host, 443)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MediaError("MEDIA_DOWNLOAD_TIMEOUT")
            connection = PinnedHTTPSConnection(host, addresses[0], min(remaining, 8))
            try:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                connection.request("GET", path, headers={"Accept": "image/png,image/jpeg,image/webp", "Accept-Encoding": "identity", "User-Agent": "Agent-Control-Media/1"})
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if redirect == 3 or not location:
                        raise MediaError("MEDIA_REDIRECT_LIMIT")
                    url = urljoin(url, location)
                    continue
                if response.status != 200 or response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise MediaError("MEDIA_DOWNLOAD_FAILED")
                length = response.getheader("Content-Length")
                if length and (not length.isdigit() or not 0 < int(length) <= maximum):
                    raise MediaError("MEDIA_FILE_LIMIT")
                result = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MediaError("MEDIA_DOWNLOAD_TIMEOUT")
                    if connection.sock:
                        connection.sock.settimeout(min(remaining, 8))
                    chunk = response.read(min(64 * 1024, maximum + 1 - len(result)))
                    if not chunk:
                        break
                    result.extend(chunk)
                    if len(result) > maximum:
                        raise MediaError("MEDIA_FILE_LIMIT")
                if not result:
                    raise MediaError("MEDIA_INVALID_IMAGE")
                return bytes(result)
            finally:
                connection.close()
        except MediaError:
            raise
        except (OSError, ValueError, http.client.HTTPException, ssl.SSLError) as error:
            raise MediaError("MEDIA_DOWNLOAD_FAILED") from error
    raise MediaError("MEDIA_REDIRECT_LIMIT")


def normalize_image(content: bytes, policy: dict | None = None) -> tuple[bytes, bytes, dict]:
    policy = validate_policy(policy)
    maximum = min(MAX_BYTES, policy["maxBytes"])
    if not isinstance(content, bytes) or not 0 < len(content) <= maximum:
        raise MediaError("MEDIA_FILE_LIMIT")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content), formats=("PNG", "JPEG", "WEBP")) as original:
                if original.width * original.height > min(MAX_PIXELS, policy["maxPixels"]) or getattr(original, "is_animated", False):
                    raise MediaError("MEDIA_PIXEL_LIMIT")
                original.load()
                image = ImageOps.exif_transpose(original)
                transparent = image.mode in {"RGBA", "LA"} or "transparency" in image.info
                image = image.convert("RGBA" if transparent else "RGB")
                # A fresh image drops EXIF, XMP, ICC and ancillary metadata.
                clean = Image.new(image.mode, image.size)
                clean.paste(image)
                output = io.BytesIO()
                format_name = "JPEG" if original.format == "JPEG" else "PNG"
                clean.save(output, format=format_name, **({"quality": 92} if format_name == "JPEG" else {}))
                normalized = output.getvalue()
                if len(normalized) > maximum:
                    raise MediaError("MEDIA_FILE_LIMIT")
                thumbnail = clean.copy()
                thumbnail.thumbnail((640, 640), Image.Resampling.LANCZOS)
                thumb_bytes = io.BytesIO()
                thumbnail.save(thumb_bytes, format="WEBP", quality=85)
                return normalized, thumb_bytes.getvalue(), {"width": clean.width, "height": clean.height,
                    "mediaType": "image/jpeg" if format_name == "JPEG" else "image/png", "thumbnailMediaType": "image/webp"}
    except MediaError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise MediaError("MEDIA_INVALID_IMAGE") from error


def profile_home(hermes_home: Path, profile: str) -> Path:
    home = hermes_home if profile == "default" else hermes_home / "profiles" / profile
    # Approved names are already validated by the runtime. Also reject local
    # filesystem aliases to another profile's outbox.
    if not home.is_dir() or home.is_symlink() or (profile != "default" and (hermes_home / "profiles").is_symlink()):
        raise MediaError("MEDIA_PROFILE_UNAVAILABLE")
    return home


def next_publication(home: Path, profile: str) -> dict | None:
    policy = load_policy(home)
    db = open_queue(home)
    try:
        with db:
            # Bound completed receipts; live conversations retain their cloud objects.
            db.execute("DELETE FROM images WHERE status != 'pending' AND created_at < ?", (time.time() - 30 * 86400,))
            db.execute("DELETE FROM images WHERE id IN (SELECT id FROM images WHERE status != 'pending' ORDER BY created_at DESC LIMIT -1 OFFSET 4096)")
            db.execute("DELETE FROM turns WHERE updated_at < ? AND session_id NOT IN (SELECT session_id FROM images WHERE status='pending')", (time.time() - 30 * 86400,))
            row = db.execute("SELECT * FROM images WHERE status='pending' AND next_attempt <= ? ORDER BY created_at LIMIT 1", (time.time(),)).fetchone()
            if row is None:
                return None
        metadata = json.loads(row["metadata"])
        content, thumbnail = row["content"], row["thumbnail"]
        if thumbnail is None:
            try:
                content = bytes(content) if content is not None else fetch_image(row["source_url"], policy)
                content, thumbnail, image_info = normalize_image(content, policy)
                metadata.update(image_info)
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    # Normalization can expand a compressed source. Recheck the
                    # aggregate while holding the same lock used by enqueuers.
                    used = db.execute("SELECT coalesce(sum(length(content)+coalesce(length(thumbnail),0)),0) FROM images WHERE status='pending' AND id!=?", (row["id"],)).fetchone()[0]
                    reserved = db.execute("SELECT count(*) FROM images WHERE status='pending' AND content IS NULL AND id!=?", (row["id"],)).fetchone()[0] * MAX_BYTES
                    if used + reserved + len(content) + len(thumbnail) > plugin.MAX_QUEUE_BYTES:
                        raise MediaError("MEDIA_OUTBOX_FULL")
                    db.execute("UPDATE images SET content=?,thumbnail=?,metadata=?,source_url=NULL WHERE id=? AND status='pending'",
                        (content, thumbnail, json.dumps(metadata), row["id"]))
            except MediaError as error:
                with db:
                    db.execute("UPDATE images SET status='failed',error_code=?,content=NULL,thumbnail=NULL,source_url=NULL WHERE id=?", (str(error), row["id"]))
                return None
        if len(content) > policy["maxBytes"] or metadata["width"] * metadata["height"] > policy["maxPixels"]:
            with db:
                db.execute("UPDATE images SET status='failed',error_code='MEDIA_POLICY_LIMIT',content=NULL,thumbnail=NULL,source_url=NULL WHERE id=?", (row["id"],))
            return None
        with db:
            # Lost acknowledgements replay exactly these immutable bytes/metadata.
            db.execute("UPDATE images SET attempts=attempts+1,next_attempt=? WHERE id=?", (time.time() + min(2 ** min(row["attempts"], 6), 60), row["id"]))
        return {"v": 1, "type": "media.publish", "id": row["id"], "profile": profile,
            "sessionId": row["session_id"], "metadata": metadata, "content": bytes(content), "thumbnail": bytes(thumbnail)}
    finally:
        db.close()


def acknowledge(home: Path, identifier: str, status: str, error_code: str | None = None):
    if status not in {"ready", "failed"}:
        raise ValueError("Invalid media acknowledgement")
    if status == "failed" and error_code in RETRYABLE_ERRORS:
        return
    db = open_queue(home)
    try:
        with db:
            db.execute("UPDATE images SET status=?,error_code=?,content=NULL,thumbnail=NULL,source_url=NULL WHERE id=? AND status='pending'",
                (status, error_code, identifier))
    finally:
        db.close()


def media_self_test():
    """Exercise compiled raster codecs and bundled plugin source without I/O."""
    from .media_install import plugin_source
    compile(plugin_source(), "agent-control-media", "exec")
    for format_name in ("PNG", "JPEG", "WEBP"):
        buffer = io.BytesIO()
        Image.new("RGB", (11, 7), "blue").save(buffer, format=format_name)
        content, thumbnail, metadata = normalize_image(buffer.getvalue())
        if metadata["width"] != 11 or metadata["height"] != 7:
            raise MediaError("MEDIA_CODEC_UNAVAILABLE")
        with Image.open(io.BytesIO(content)) as image:
            image.load()
        with Image.open(io.BytesIO(thumbnail)) as image:
            if image.format != "WEBP":
                raise MediaError("MEDIA_CODEC_UNAVAILABLE")
            image.load()
