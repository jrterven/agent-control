from __future__ import annotations

import base64
import json
import re
from collections.abc import Sequence
from typing import Any

from .types import PromptAttachment, PromptAttachmentReceipt
from .prompt_display import project_managed_context


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
    media_ids: Sequence[str | None] = (),
) -> str:
    """Add bounded display metadata and private Hermes file references."""

    metadata = [
        {
            "kind": item.kind,
            "name": item.name,
            "mediaType": item.media_type,
            "size": len(item.content),
            **({"mediaId": media_ids[index]} if index < len(media_ids) and media_ids[index] else {}),
        }
        for index, item in enumerate(attachments[:5])
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

    content = project_managed_context(content)
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
            {"kind": kind, "name": name, "mediaType": media_type, "size": size,
             **({"mediaId": row["mediaId"]} if kind == "image" and isinstance(row.get("mediaId"), str)
                and re.fullmatch(r"[0-9a-f]{32}", row["mediaId"]) else {})}
        )
    body = content[match.end():]
    body, _ = _image_suffix(body, sum(item["kind"] == "image" for item in attachments))
    cleaned = _REFS_BLOCK.sub("", body).strip()
    if cleaned.startswith(f"{_EMPTY_PROMPT}\n"):
        cleaned = ""
    return cleaned, attachments


_IMAGE_HINT = re.compile(r"\[Image attached at: ([^\n\]]{1,4096})\]")
_IMAGE_REF = re.compile(r'@image:(`[^`\n]+`|"[^"\n]+"|\x27[^\x27\n]+\x27|\S+)')


def _image_suffix(body: str, count: int) -> tuple[str, list[str]]:
    """Only consume a complete trailing native image block of the expected size."""
    if not count:
        return body, []
    lines = body.rstrip().split("\n")
    placeholders = 0
    while lines and lines[-1] in {"[screenshot]", "[image]"}:
        lines.pop()
        placeholders += 1
    if placeholders not in (0, count) or len(lines) < count:
        return body, []
    paths = []
    for line in lines[-count:]:
        hint = _IMAGE_HINT.fullmatch(line)
        reference = _IMAGE_REF.fullmatch(line)
        if hint:
            paths.append(hint[1])
        elif reference:
            value = reference[1]
            paths.append(value[1:-1] if value[0] in ('`', '"', "'") else value)
        else:
            return body, []
    return "\n".join(lines[:-count]).rstrip(), paths


def attachment_image_paths(content: str) -> list[tuple[int, str]]:
    """Private native paths, paired only with validated Control attachment metadata."""
    content = project_managed_context(content)
    match = _META_LINE.match(content)
    _, attachments = project_attachment_prompt(content)
    indices = [index for index, item in enumerate(attachments) if item["kind"] == "image"]
    if match is None or not indices:
        return []
    _, paths = _image_suffix(content[match.end():], len(indices))
    return list(zip(indices, paths, strict=True)) if len(paths) == len(indices) else []


def prompt_without_native_suffix(content: str) -> str:
    """Recover submitted bytes for reconciliation, retaining attachment identity."""
    content = project_managed_context(content)
    match = _META_LINE.match(content)
    _, attachments = project_attachment_prompt(content)
    if match is None or not attachments:
        return content
    body, _ = _image_suffix(content[match.end():], sum(item["kind"] == "image" for item in attachments))
    return content[:match.end()] + body
