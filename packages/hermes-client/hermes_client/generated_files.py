"""Conversation-scoped deliverables, explicitly staged by the agent for download."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata
from urllib.parse import unquote

MAX_FILE_BYTES = 50 * 1024 * 1024
FILE_LINK = re.compile(r"(?<=\]\()ac-file:([^\s()<>]{1,768})(?=\))")
_CONTEXT = (
    "Agent Control file delivery: when you create a document or other downloadable "
    "deliverable for the user, also save a regular copy in the download directory below. "
    "Create the directory if needed. Use a unique filename, no subdirectories or symlinks, "
    "and at most 50 MiB per file. Keep any user-requested original location too. "
    "After verifying the saved copy, include [Download filename](ac-file:filename) in "
    "your final answer, translating the label to the user's language and URL-encoding "
    "the filename (including spaces and parentheses). Never invent a download link or "
    "give only a local path. Only stage deliverables requested by the user, never "
    "credentials or unrelated private files. If a worker creates the file, have it "
    "return the result and stage the copy in this parent conversation's directory. "
    "Downloads require this computer to remain connected; do not promise offline access."
)
_PREFIX = "<!-- agent-control-downloads-v1\n" + _CONTEXT + "\nDirectory (JSON): "
_SUFFIX = "\n-->\n\n"


def valid_filename(name: object) -> bool:
    try:
        size = len(name.encode("utf-8")) if isinstance(name, str) else 0
    except UnicodeError:
        return False
    return (isinstance(name, str) and 0 < size <= 240
            and not name.startswith(".") and name == name.strip()
            and not any(c in '/\\' or unicodedata.category(c).startswith("C") for c in name))


def file_directory(home: Path, profile: str, session: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", profile):
        raise ValueError("Invalid profile")
    profile_home = home if profile == "default" else home / "profiles" / profile
    key = hashlib.sha256(session.encode()).hexdigest()[:32]
    return profile_home / "exports" / key


def with_file_context(prompt: str, home: Path, profile: str, session: str) -> str:
    if session.startswith("ac_tmp_"):
        return prompt
    directory = str(file_directory(home, profile, session))
    return _PREFIX + json.dumps(directory, ensure_ascii=True) + _SUFFIX + prompt


def project_file_context(content: str) -> str:
    if not content.startswith(_PREFIX):
        return content
    payload, separator, body = content[len(_PREFIX):].partition(_SUFFIX)
    try:
        directory = json.loads(payload) if separator and len(payload) <= 8192 else None
        if (isinstance(directory, str) and directory.startswith("/")
                and re.search(r"/exports/[a-f0-9]{32}$", directory)
                and json.dumps(directory, ensure_ascii=True) == payload):
            return body
    except (ValueError, TypeError):
        pass
    return content


def file_id(profile: str, session: str, name: str) -> str:
    return hashlib.sha256(f"file\0{profile}\0{session}\0{name}".encode()).hexdigest()[:32]


def _open_file(home: Path, profile: str, session: str, name: str) -> int:
    """Open relative to trusted home; never follow directory or file aliases."""
    if not valid_filename(name) or session.startswith("ac_tmp_"):
        raise ValueError("File unavailable")
    path = file_directory(home, profile, session) / name
    directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.relative_to(home).parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def _check_file(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 <= info.st_size <= MAX_FILE_BYTES:
        raise ValueError("File unavailable")
    return info


def file_references(content: str):
    for match in list(FILE_LINK.finditer(content))[:16]:
        name = unquote(match[1])
        if valid_filename(name):
            yield match, name


def project_files(history: list, home: Path, profile: str, session: str) -> list:
    result = [dict(item) if isinstance(item, dict) else item for item in history]
    for item in result:
        if not isinstance(item, dict):
            continue
        item.pop("controlFiles", None)
        if session.startswith("ac_tmp_") or item.get("role") != "assistant":
            continue
        content = item.get("content", item.get("text"))
        if not isinstance(content, str):
            continue
        seen = set()
        for _, name in file_references(content):
            if name in seen:
                continue
            seen.add(name)
            try:
                fd = _open_file(home, profile, session, name)
                try:
                    info = _check_file(fd)
                finally:
                    os.close(fd)
            except (OSError, ValueError):
                continue
            item.setdefault("controlFiles", []).append({
                "id": file_id(profile, session, name), "name": name, "size": info.st_size,
            })
    return result


def read_file(history: list, home: Path, profile: str, session: str, identifier: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", identifier) or session.startswith("ac_tmp_"):
        raise LookupError("File unavailable")
    for item in history:
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        content = item.get("content", item.get("text"))
        if not isinstance(content, str):
            continue
        for _, name in file_references(content):
            if file_id(profile, session, name) != identifier:
                continue
            try:
                with os.fdopen(_open_file(home, profile, session, name), "rb") as source:
                    _check_file(source.fileno())
                    data = source.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise ValueError("File unavailable")
            except (OSError, ValueError):
                raise LookupError("File unavailable") from None
            return {"content": data, "media_type": "application/octet-stream", "filename": name}
    raise LookupError("File unavailable")
