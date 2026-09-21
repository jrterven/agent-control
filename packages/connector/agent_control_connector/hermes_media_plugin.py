"""Standalone Hermes plugin, copied verbatim by the connector (stdlib only).

This module deliberately has no connector or cloud imports. It can enqueue media
while the connector is disconnected, without receiving cloud credentials.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import types
import time
from uuid import uuid4

PLUGIN_NAME = "agent-control-media"
PLUGIN_VERSION = "1.1.0"
MAX_BYTES = 10 * 1024 * 1024
MAX_QUEUE_BYTES = 256 * 1024 * 1024
MAX_QUEUE_ITEMS = 512
DEFAULT_POLICY = {"maxBytes": MAX_BYTES, "maxPixels": 25_000_000,
    "maxImagesPerGallery": 6, "maxImagesPerResponse": 24}
INSTRUCTIONS = """Agent Control images: use publish_images when pictures, paper figures, charts or screenshots add useful information. Use existing search, capture or generation tools to obtain them. Publish local PNG/JPEG/WebP files or HTTPS image URLs, with accurate alt text, optional caption and source URL/title. Label provenance web, generated or local truthfully. The tool returns private references: insert its exact ![description](ac-media:ID) Markdown in your answer where the picture belongs. Consecutive images form a gallery (at most six); use at most 24 images per answer. Publication may be pending while the connector reconnects; never claim pending images are published. Do not invent IDs, expose local paths, inline base64, remote image links or HTML. Cite the source page separately when useful. Images are optional; do not add decorative or irrelevant pictures."""

SCHEMA = {"name": "publish_images", "description": "Publish up to six images privately in the current Agent Control conversation. Returns Markdown and publication status.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["images"], "properties": {
        "images": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "object", "additionalProperties": False,
            "required": ["alt", "provenance"], "properties": {
                "path": {"type": "string", "description": "Local raster image path; specify path or url, not both."},
                "url": {"type": "string", "description": "Public HTTPS image URL; never a source page HTML URL."},
                "alt": {"type": "string", "maxLength": 1000}, "caption": {"type": "string", "maxLength": 2000},
                "sourceUrl": {"type": "string", "maxLength": 2048}, "sourceTitle": {"type": "string", "maxLength": 300},
                "provenance": {"type": "string", "enum": ["web", "generated", "local"]}}}}}}}


def validate_policy(value: dict | None) -> dict:
    if value is None:
        return dict(DEFAULT_POLICY)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ValueError("MEDIA_POLICY_UNAVAILABLE")
    policy = {**DEFAULT_POLICY, **value}
    if any(type(policy[key]) is not int or not 1 <= policy[key] <= maximum for key, maximum in DEFAULT_POLICY.items()):
        raise ValueError("MEDIA_POLICY_UNAVAILABLE")
    # A gallery cannot contain more images than one response permits.
    policy["maxImagesPerGallery"] = min(policy["maxImagesPerGallery"], policy["maxImagesPerResponse"])
    return policy


def load_policy(home: Path) -> dict:
    path = home / ".agent-control/media/policy.json"
    if not path.exists():
        return dict(DEFAULT_POLICY)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2048:
        raise ValueError("MEDIA_POLICY_UNAVAILABLE")
    try:
        return validate_policy(json.loads(path.read_text()))
    except (OSError, ValueError) as error:
        raise ValueError("MEDIA_POLICY_UNAVAILABLE") from error


def queue_directory(home: Path) -> Path:
    # Never follow a substituted private outbox directory or database.
    for current in (home / ".agent-control", home / ".agent-control/media"):
        current.mkdir(mode=0o700, exist_ok=True)
        if current.is_symlink() or current.stat().st_uid != os.getuid():
            raise ValueError("MEDIA_OUTBOX_UNSAFE")
        current.chmod(0o700)
    return home / ".agent-control/media"


def open_queue(home: Path) -> sqlite3.Connection:
    module = sys.modules.get("agent_control_chat_policy")
    runtime = getattr(module, "active_runtime", None)
    db = runtime.private_media_queue() if runtime is not None else None
    if db is None:
        path = queue_directory(home) / "outbox.sqlite3"
        if path.is_symlink():
            raise ValueError("MEDIA_OUTBOX_UNSAFE")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        path.chmod(0o600)
        db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA secure_delete=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS images (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
          metadata TEXT NOT NULL, source_url TEXT, content BLOB, thumbnail BLOB,
          status TEXT NOT NULL DEFAULT 'pending', error_code TEXT,
          created_at REAL NOT NULL, next_attempt REAL NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS turns (session_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL,
          image_count INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0);
    """)
    return db


def _text(value, limit, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not 0 < len(value.strip()) <= limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError("MEDIA_INVALID_METADATA")
    return value.strip()


def _source_url(value):
    from urllib.parse import urlsplit
    result = _text(value, 2048)
    if result is None:
        return None
    parsed = urlsplit(result)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or any(ord(c) < 32 for c in result):
        raise ValueError("MEDIA_INVALID_URL")
    return result


def enqueue_images(home: Path, session_id: str, images: list) -> list[dict]:
    policy = load_policy(home)
    if not isinstance(session_id, str) or not 1 <= len(session_id) <= 200 or any(ord(c) < 32 for c in session_id):
        raise ValueError("MEDIA_SESSION_UNAVAILABLE")
    if not isinstance(images, list) or not 1 <= len(images) <= policy["maxImagesPerGallery"]:
        raise ValueError("MEDIA_GALLERY_LIMIT")
    prepared = []
    for item in images:
        if not isinstance(item, dict) or set(item) - {"path", "url", "alt", "caption", "sourceUrl", "sourceTitle", "provenance"}:
            raise ValueError("MEDIA_INVALID_METADATA")
        if bool(item.get("path")) == bool(item.get("url")):
            raise ValueError("MEDIA_SOURCE_REQUIRED")
        if item.get("provenance") not in {"web", "generated", "local"}:
            raise ValueError("MEDIA_INVALID_METADATA")
        metadata = {"alt": _text(item.get("alt"), 1000, required=True), "provenance": item["provenance"]}
        for key, limit in (("caption", 2000), ("sourceTitle", 300)):
            value = _text(item.get(key), limit)
            if value:
                metadata[key] = value
        source_url = _source_url(item.get("url"))
        provenance_url = _source_url(item.get("sourceUrl")) or source_url
        if provenance_url:
            metadata["sourceUrl"] = provenance_url
        elif item["provenance"] == "web":
            raise ValueError("MEDIA_SOURCE_URL_REQUIRED")
        content = None
        if not source_url:
            path = Path(_text(item.get("path"), 4096, required=True)).expanduser()
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as file:
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= policy["maxBytes"]:
                    raise ValueError("MEDIA_FILE_LIMIT")
                content = file.read(policy["maxBytes"] + 1)
            if not 0 < len(content) <= policy["maxBytes"]:
                raise ValueError("MEDIA_FILE_LIMIT")
        prepared.append((uuid4().hex, metadata, source_url, content))
    db = open_queue(home)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            turn = db.execute("SELECT turn_id,image_count FROM turns WHERE session_id = ?", (session_id,)).fetchone()
            if not turn:
                raise ValueError("MEDIA_TURN_UNAVAILABLE")
            turn_id = turn[0]
            if turn["image_count"] + len(prepared) > policy["maxImagesPerResponse"]:
                raise ValueError("MEDIA_RESPONSE_LIMIT")
            queued, size = db.execute("SELECT count(*), coalesce(sum(length(content)+coalesce(length(thumbnail),0)),0) FROM images WHERE status = 'pending'").fetchone()
            # Reserve the maximum for URLs, which have not been downloaded yet.
            reserved = db.execute("SELECT count(*) FROM images WHERE status='pending' AND content IS NULL").fetchone()[0] * MAX_BYTES
            if queued + len(prepared) > MAX_QUEUE_ITEMS or size + reserved + sum(len(p[3]) if p[3] else MAX_BYTES for p in prepared) > MAX_QUEUE_BYTES:
                raise ValueError("MEDIA_OUTBOX_FULL")
            for identifier, metadata, source_url, content in prepared:
                db.execute("INSERT INTO images(id,session_id,turn_id,metadata,source_url,content,created_at) VALUES(?,?,?,?,?,?,?)",
                    (identifier, session_id, turn_id, json.dumps(metadata), source_url, content, time.time()))
            db.execute("UPDATE turns SET image_count=image_count+?,updated_at=? WHERE session_id=?", (len(prepared), time.time(), session_id))
        references = []
        for identifier, metadata, _, _ in prepared:
            alt = re.sub(r'[\[\]\\\n\r]', ' ', metadata["alt"])
            references.append({"id": identifier, "status": "pending", "markdown": f"![{alt}](ac-media:{identifier})"})
        return references
    finally:
        db.close()


def register(ctx):
    from hermes_constants import get_hermes_home
    home = get_hermes_home().resolve()
    marker = sys.modules.setdefault("agent_control_private_media", types.ModuleType("agent_control_private_media"))
    if not hasattr(marker, "homes"):
        marker.homes = set()
    marker.homes.add(str(home))

    def instructions():
        policy = load_policy(home)
        return INSTRUCTIONS + (f" Current account limits: {policy['maxImagesPerGallery']} images per gallery, "
            f"{policy['maxImagesPerResponse']} per response, {policy['maxBytes']} bytes and {policy['maxPixels']} pixels per image.")

    def platform_disabled(platform):
        try:
            from hermes_cli.config import load_config
            config = load_config()
            plugins = config.get("plugins") or {}
            if PLUGIN_NAME in (plugins.get("disabled") or []) or PLUGIN_NAME not in (plugins.get("enabled") or []):
                return True
            for section in (config.get("agent") or {}, config.get("tools") or {}):
                denied = section.get("disabled_toolsets") or []
                if isinstance(denied, str):
                    denied = [part.strip() for part in denied.split(",")]
                if PLUGIN_NAME in denied or "publish_images" in denied:
                    return True
            known = (config.get("known_plugin_toolsets") or {}).get(platform) or []
            selected = (config.get("platform_toolsets") or {}).get(platform) or []
            return PLUGIN_NAME in known and PLUGIN_NAME not in selected
        except (ImportError, AttributeError, TypeError):
            return False

    def before_turn(session_id="", turn_id="", platform="", **_):
        if platform_disabled(platform):
            return None
        if session_id and turn_id:
            db = open_queue(home)
            try:
                with db:
                    db.execute("""INSERT INTO turns(session_id,turn_id,updated_at) VALUES(?,?,?)
                        ON CONFLICT(session_id) DO UPDATE SET turn_id=excluded.turn_id,
                        image_count=CASE WHEN turn_id=excluded.turn_id THEN image_count ELSE 0 END,
                        updated_at=excluded.updated_at""", (session_id, turn_id, time.time()))
            finally:
                db.close()
        return {"context": instructions()}

    def publish(args, session_id="", **_):
        try:
            if platform_disabled(""):
                raise ValueError("MEDIA_DISABLED")
            if not isinstance(args, dict) or set(args) != {"images"}:
                raise ValueError("MEDIA_INVALID_ARGUMENTS")
            rows = enqueue_images(home, session_id, args["images"])
            # Give the connected outbox a brief chance to acknowledge, without
            # making scheduling depend on the browser or cloud availability.
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with closing(open_queue(home)) as db:
                    states = {r["id"]: r for r in db.execute("SELECT id,status,error_code FROM images WHERE id IN (%s)" % ",".join("?" * len(rows)), [r["id"] for r in rows])}
                for row in rows:
                    state = states.get(row["id"])
                    if state:
                        row["status"] = "published" if state["status"] == "ready" else state["status"]
                        if state["error_code"]:
                            row["errorCode"] = state["error_code"]
                if all(r["status"] != "pending" for r in rows):
                    break
                time.sleep(.1)
            return json.dumps({"images": rows})
        except (ValueError, OSError, sqlite3.Error) as error:
            code = str(error) if re.fullmatch(r"MEDIA_[A-Z_]+", str(error)) else "MEDIA_PUBLICATION_FAILED"
            return json.dumps({"error": code})

    def before_tool(tool_name="", args=None, **_):
        if tool_name == "cronjob" and isinstance(args, dict) and not platform_disabled("cron"):
            enabled = args.get("enabled_toolsets")
            if isinstance(enabled, list) and enabled and PLUGIN_NAME not in enabled:
                return {"action": "modify", "args": {"enabled_toolsets": [*enabled, PLUGIN_NAME]}}
        return None

    handle = ctx.register_tool("publish_images", PLUGIN_NAME, SCHEMA, publish, description=SCHEMA["description"])
    if handle is None:
        raise ValueError("MEDIA_TOOL_REGISTRATION_FAILED")
    ctx.register_system_prompt_section("agent-control-media",
        lambda info: "" if platform_disabled(info.get("platform", "")) else instructions(), max_chars=3000)
    ctx.register_hook("pre_llm_call", before_turn)
    ctx.register_hook("pre_tool_call", before_tool)
    # Live marker is diagnostic only, never an authorization capability.
    marker = queue_directory(home) / "runtime.json"
    temporary = marker.with_name(f"runtime-{uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump({"pid": os.getpid(), "version": PLUGIN_VERSION,
            "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "loadedAt": time.time()}, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, marker)
