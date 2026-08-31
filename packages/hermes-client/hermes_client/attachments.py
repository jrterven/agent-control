from __future__ import annotations

import base64
import json
import re
from collections.abc import Sequence
from typing import Any

from .types import PromptAttachment, PromptAttachmentReceipt


_META_PREFIX = "<!-- hermes-control-attachments-v1:"
_META_SUFFIX = " -->"
_REFS_START = "<!-- hermes-control-file-refs-v1 -->"
_REFS_END = "<!-- /hermes-control-file-refs-v1 -->"
_EMPTY_PROMPT = "<!-- hermes-control-empty-prompt-v1 -->"
_META_LINE = re.compile(
    rf"^{re.escape(_META_PREFIX)}(?P<payload>[A-Za-z0-9_-]{{1,4096}}){re.escape(_META_SUFFIX)}(?:\n|$)"
)
_REFS_BLOCK = re.compile(
    rf"\n?{re.escape(_REFS_START)}\n.*?\n{re.escape(_REFS_END)}\s*$",
    re.DOTALL,
)


def compose_attachment_prompt(
    prompt: str,
    attachments: Sequence[PromptAttachment],
    receipts: Sequence[PromptAttachmentReceipt],
) -> str:
    """Add bounded display metadata and private Hermes file references."""

    metadata = [
        {
            "kind": item.kind,
            "name": item.name,
            "mediaType": item.media_type,
            "size": len(item.content),
        }
        for item in attachments[:5]
    ]
    encoded = base64.urlsafe_b64encode(
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    body = prompt.strip() or f"{_EMPTY_PROMPT}\nPlease review the attached item(s)."
    references = [item.reference for item in receipts if item.reference]
    rendered = f"{_META_PREFIX}{encoded}{_META_SUFFIX}\n{body}"
    if references:
        rendered += f"\n\n{_REFS_START}\n" + "\n".join(references) + f"\n{_REFS_END}"
    return rendered


def project_attachment_prompt(content: str) -> tuple[str, list[dict[str, Any]]]:
    """Remove private hand-off markers and return safe attachment metadata."""

    match = _META_LINE.match(content)
    if match is None:
        return content, []
    try:
        payload = match.group("payload")
        decoded = base64.urlsafe_b64decode(payload + ("=" * (-len(payload) % 4)))
        rows = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return content, []
    if not isinstance(rows, list) or len(rows) > 5:
        return content, []
    attachments: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            return content, []
        kind = row.get("kind")
        name = row.get("name")
        media_type = row.get("mediaType")
        size = row.get("size")
        if (
            kind not in {"image", "file"}
            or not isinstance(name, str)
            or not name
            or len(name) > 120
            or not isinstance(media_type, str)
            or len(media_type) > 120
            or not isinstance(size, int)
            or size < 1
            or size > 8 * 1024 * 1024
        ):
            return content, []
        attachments.append(
            {"kind": kind, "name": name, "mediaType": media_type, "size": size}
        )
    cleaned = _REFS_BLOCK.sub("", content[match.end() :]).strip()
    if cleaned.startswith(f"{_EMPTY_PROMPT}\n"):
        cleaned = ""
    return cleaned, attachments
