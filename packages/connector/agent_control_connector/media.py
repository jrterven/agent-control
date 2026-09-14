"""Read-only, transcript-bound local audio. No caller-supplied filesystem paths."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import re
import stat

from hermes_client.connector_protocol import MAX_BLOB_BYTES

TYPES = {".aac": "audio/aac", ".flac": "audio/flac", ".m4a": "audio/mp4", ".mp3": "audio/mpeg",
         ".oga": "audio/ogg", ".ogg": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav", ".webm": "audio/webm"}
EXT = "|".join(suffix[1:] for suffix in TYPES)
MARKER = re.compile(r'''[`"'*_]{0,3}MEDIA:\s*(?P<path>`[^`\r\n]+?\.(?:''' + EXT + r''')`|"[^"\r\n]+?\.(?:''' + EXT + r''')"|'[^'\r\n]+?\.(?:''' + EXT + r''')'|(?:~/|/)\S+?\.(?:''' + EXT + r'''))(?=[\s`"'*_,;:)\]}\[]|MEDIA:|$)[`"'*_]{0,3}\.?''', re.IGNORECASE)


def candidates(history, hermes_home: Path, profile: str, session_id: str):
    roots = [hermes_home / "profiles" / profile / "cache" / "audio"]
    if profile == "default":
        roots.append(hermes_home / "cache" / "audio")
    valid_roots = []
    for root in roots:
        try:
            relative = root.relative_to(hermes_home)
            current = hermes_home
            for component in relative.parts:
                current = current / component
                mode = current.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    raise ValueError("Invalid audio root")
            valid_roots.append(root.resolve(strict=True))
        except (OSError, ValueError):
            continue
    for index, item in enumerate(history):
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        content = item.get("content", item.get("text"))
        if not isinstance(content, str):
            continue
        for media_index, match in enumerate(list(MARKER.finditer(content))[:16]):
            path = Path(match.group("path").strip("`\"'")).expanduser()
            try:
                resolved = path.resolve(strict=True)
                if not any(resolved.is_relative_to(root) for root in valid_roots):
                    continue
                info = resolved.stat()
                media_type = TYPES.get(resolved.suffix.lower())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BLOB_BYTES or not media_type:
                    continue
            except (OSError, ValueError, RuntimeError):
                continue
            media_id = hashlib.sha256(f"{profile}\0{session_id}\0{index}\0{media_index}\0{resolved}".encode()).hexdigest()[:32]
            yield index, match, resolved, media_id, media_type


def project_media(history, hermes_home: Path, profile: str, session_id: str):
    result = [dict(item) if isinstance(item, dict) else item for item in history]
    for item in result:
        if isinstance(item, dict):
            item.pop("controlMedia", None)
    collected = {}
    for index, match, _, media_id, media_type in candidates(history, hermes_home, profile, session_id):
        result[index].setdefault("controlMedia", []).append({"id": media_id, "kind": "audio", "mediaType": media_type})
        collected.setdefault(index, []).append(match)
    for index, matches in collected.items():
        key = "content" if isinstance(result[index].get("content"), str) else "text"
        content = result[index][key]
        for match in reversed(matches):
            content = content[:match.start()] + content[match.end():]
        result[index][key] = content.strip()
    return result


def _open_beneath(path: Path, root: Path) -> int:
    """Anchor every path component with directory descriptors, rejecting symlinks."""
    components = path.relative_to(root).parts
    if not components:
        raise LookupError("Voice note not found")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in components[:-1]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        return os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    finally:
        os.close(directory)


def read_media(history, hermes_home: Path, profile: str, session_id: str, media_id: str):
    if not isinstance(media_id, str) or not re.fullmatch(r"[a-f0-9]{32}", media_id):
        raise LookupError("Voice note not found")
    for _, _, path, identifier, media_type in candidates(history, hermes_home, profile, session_id):
        if identifier != media_id:
            continue
        # O_NOFOLLOW closes final-component symlink swaps after containment check.
        fd = _open_beneath(path, hermes_home)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise LookupError("Voice note not found")
            content = source.read(MAX_BLOB_BYTES + 1)
        if not 0 < len(content) <= MAX_BLOB_BYTES:
            raise ValueError("Voice note exceeds transfer limit")
        return {"content": content, "media_type": media_type}
    raise LookupError("Voice note not found")
