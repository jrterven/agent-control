from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit


_INSTRUCTION_START = "<hermes-control-email-ui-instruction-v1>"
_INSTRUCTION_END = "</hermes-control-email-ui-instruction-v1>"
_INSTRUCTION_BLOCK = re.compile(
    rf"\n?{re.escape(_INSTRUCTION_START)}.*?{re.escape(_INSTRUCTION_END)}\s*",
    re.DOTALL,
)
EMAIL_REFERENCE_MARKER_NAME = "hermes-control-email-reference-v1"
EMAIL_REFERENCE_MARKER_PREFIX = f"<!-- {EMAIL_REFERENCE_MARKER_NAME}"
_REFERENCE_START = re.compile(
    rf"<!--\s*{re.escape(EMAIL_REFERENCE_MARKER_NAME)}\b",
    re.IGNORECASE,
)
_REFERENCE_TAIL = re.compile(
    rf"<!--\s*{re.escape(EMAIL_REFERENCE_MARKER_NAME)}\b.*$",
    re.DOTALL | re.IGNORECASE,
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_EMAIL_ADDRESS = re.compile(r"^[^\s<>@]{1,128}@[^\s<>@]{1,253}$")
_MAX_REFERENCES = 8
_MAX_MARKER_BYTES = 64 * 1024


EMAIL_REFERENCE_INSTRUCTION = f"""{_INSTRUCTION_START}
When citing a specific email returned by a mail tool, append after the answer:
<!-- hermes-control-email-reference-v1 {{"provider":"gmail|outlook|imap","accountAddress":"optional","account":"optional","mailbox":"optional","uid":"optional IMAP UID","messageId":"optional RFC Message-ID","senderName":"optional","senderAddress":"optional","subject":"required","receivedAt":"optional ISO-8601","snippet":"optional","bodyText":"optional tool-returned plain text, max 12000 chars","sourceUrl":"optional Outlook Graph webLink"}} -->
Use tool facts only. Require messageId, a provider read URL, or
uid+mailbox+account/accountAddress. Omit unknowns; never include auth or reasoning.
Do not mention the marker.
{_INSTRUCTION_END}"""


@dataclass(frozen=True, slots=True)
class EmailReferenceCandidate:
    """Bounded mail metadata carried through an untrusted agent transcript.

    Candidate data is display evidence, not proof that the upstream mailbox
    contains the message.  Provider URLs cross a stricter allowlist before an
    authenticated Control endpoint may redirect to them.
    """

    provider: str
    account_address: str | None
    account: str | None
    mailbox: str | None
    uid: str | None
    message_id: str | None
    sender_name: str | None
    sender_address: str | None
    subject: str
    received_at: str | None
    snippet: str | None
    body_text: str | None
    source_url: str | None
    open_mode: str | None

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            {
                "provider": self.provider,
                "accountAddress": self.account_address,
                "account": self.account,
                "mailbox": self.mailbox,
                "uid": self.uid,
                "messageId": self.message_id,
                "senderName": self.sender_name,
                "senderAddress": self.sender_address,
                "subject": self.subject,
                "receivedAt": self.received_at,
                "snippet": self.snippet,
                "bodyText": self.body_text,
                "sourceUrl": self.source_url,
                "openMode": self.open_mode,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def public_view(
        self,
        *,
        reference_id: str | None = None,
        preview_url: str | None = None,
        open_url: str | None = None,
        include_body: bool = False,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schemaVersion": 1,
            "provider": self.provider,
            "subject": self.subject,
        }
        if reference_id:
            result["id"] = reference_id
        for key, value in (
            ("senderName", self.sender_name),
            ("senderAddress", self.sender_address),
            ("receivedAt", self.received_at),
            ("snippet", self.snippet),
            ("previewUrl", preview_url),
            ("openUrl", open_url),
            ("openMode", self.open_mode if open_url else None),
        ):
            if value:
                result[key] = value
        if include_body and self.body_text:
            result["bodyText"] = self.body_text
        return result

    def transport_view(self) -> dict[str, Any]:
        """Safe pre-binding view; the browser binder replaces the fingerprint."""

        return {**self.public_view(), "_fingerprint": self.fingerprint}

    def private_payload(self) -> dict[str, Any]:
        """Return the validated candidate for encrypted server-side caching."""

        return {
            "provider": self.provider,
            "accountAddress": self.account_address,
            "account": self.account,
            "mailbox": self.mailbox,
            "uid": self.uid,
            "messageId": self.message_id,
            "senderName": self.sender_name,
            "senderAddress": self.sender_address,
            "subject": self.subject,
            "receivedAt": self.received_at,
            "snippet": self.snippet,
            "bodyText": self.body_text,
            "sourceUrl": self.source_url,
        }


@dataclass(frozen=True, slots=True)
class ParsedEmailReferenceMarker:
    end: int
    candidate: EmailReferenceCandidate | None


def compose_email_reference_prompt(prompt: str) -> str:
    """Attach the versioned UI protocol without altering visible history."""

    cleaned = project_email_reference_prompt(prompt)[0]
    return f"{cleaned}\n\n{EMAIL_REFERENCE_INSTRUCTION}"


def has_email_reference_instruction(content: str) -> bool:
    return _INSTRUCTION_START in content and _INSTRUCTION_END in content


def project_email_reference_prompt(content: str) -> tuple[str, list[EmailReferenceCandidate]]:
    """Remove Control-only protocol text and parse bounded assistant markers."""

    lowered = content.casefold()
    if (
        "hermes-control-email-reference-v1" not in lowered
        and _INSTRUCTION_START not in lowered
    ):
        return content, []
    without_instruction = _INSTRUCTION_BLOCK.sub("\n", content)
    candidates: list[EmailReferenceCandidate] = []
    pieces: list[str] = []
    cursor = 0
    for _ in range(_MAX_REFERENCES):
        match = _REFERENCE_START.search(without_instruction, cursor)
        if match is None:
            break
        pieces.append(without_instruction[cursor : match.start()])
        parsed = parse_email_reference_marker(without_instruction, match.start())
        if parsed is None:
            cursor = len(without_instruction)
            break
        cursor = parsed.end
        candidate = parsed.candidate
        if candidate is not None and candidate.fingerprint not in {
            item.fingerprint for item in candidates
        }:
            candidates.append(candidate)
    pieces.append(without_instruction[cursor:])
    # Remove any additional markers beyond the public cardinality limit rather
    # than leaking their untrusted transport payload into the transcript.
    cleaned = _REFERENCE_TAIL.sub("", "".join(pieces)).strip()
    return cleaned, candidates


def parse_email_reference_marker(
    content: str,
    start: int = 0,
) -> ParsedEmailReferenceMarker | None:
    """Parse one exact marker without treating terminators inside JSON as HTML."""

    marker_start = _REFERENCE_START.match(content, start)
    if marker_start is None:
        return None
    payload_start = marker_start.end()
    while payload_start < len(content) and content[payload_start].isspace():
        payload_start += 1
    payload_window = content[payload_start : payload_start + _MAX_MARKER_BYTES + 1]
    try:
        decoded, relative_end = json.JSONDecoder().raw_decode(payload_window)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    payload = payload_window[:relative_end]
    if len(payload.encode("utf-8")) > _MAX_MARKER_BYTES:
        return None
    marker_end = payload_start + relative_end
    while marker_end < len(content) and content[marker_end].isspace():
        marker_end += 1
    if not content.startswith("-->", marker_end):
        return None
    return ParsedEmailReferenceMarker(
        end=marker_end + 3,
        candidate=email_reference_candidate(decoded),
    )


def email_reference_candidates(value: Any) -> list[EmailReferenceCandidate]:
    """Project a future structured Hermes field through the same contract."""

    rows: Sequence[Any]
    if isinstance(value, (list, tuple)):
        rows = value[:_MAX_REFERENCES]
    elif isinstance(value, Mapping):
        rows = [value]
    else:
        return []
    projected: list[EmailReferenceCandidate] = []
    seen: set[str] = set()
    for row in rows:
        candidate = email_reference_candidate(row)
        if candidate is None or candidate.fingerprint in seen:
            continue
        seen.add(candidate.fingerprint)
        projected.append(candidate)
    return projected


def email_reference_candidate(value: Any) -> EmailReferenceCandidate | None:
    if not isinstance(value, Mapping):
        return None
    provider = _single_line(value.get("provider"), 20)
    if provider is None:
        return None
    provider = provider.casefold()
    if provider in {"google", "google-mail"}:
        provider = "gmail"
    elif provider in {"microsoft", "microsoft-365", "office365"}:
        provider = "outlook"
    elif provider in {"generic", "himalaya", "hostinger"}:
        provider = "imap"
    if provider not in {"gmail", "outlook", "imap"}:
        return None

    subject = _single_line(value.get("subject"), 500)
    if not subject:
        return None
    sender = value.get("from") if isinstance(value.get("from"), Mapping) else {}
    sender_name = _single_line(
        value.get("senderName") or value.get("sender_name") or sender.get("name"),
        200,
    )
    sender_address = _single_line(
        value.get("senderAddress")
        or value.get("sender_address")
        or sender.get("address")
        or sender.get("email"),
        384,
    )
    if sender_address and not _EMAIL_ADDRESS.fullmatch(sender_address):
        sender_address = None
    body_text = _body_text(
        value.get("bodyText") or value.get("body_text") or value.get("textBody")
    )
    snippet = _single_line(
        value.get("snippet") or value.get("preview") or value.get("summary"),
        700,
    )
    account_address = _single_line(
        value.get("accountAddress") or value.get("account_address"), 384
    )
    if account_address and not _EMAIL_ADDRESS.fullmatch(account_address):
        account_address = None
    account = _single_line(value.get("account") or value.get("accountName"), 200)
    mailbox = _single_line(value.get("mailbox") or value.get("folder"), 255)
    uid = _uid(value.get("uid"))
    message_id = _message_id(
        value.get("messageId")
        or value.get("message_id")
        or value.get("internetMessageId")
        or value.get("internet_message_id")
    )
    source_url = (
        safe_email_open_url(
            provider,
            value.get("sourceUrl")
            or value.get("source_url")
            or value.get("webUrl")
            or value.get("web_url")
            or value.get("openUrl"),
        )
        if provider == "outlook"
        else None
    )
    has_imap_identity = bool(uid and mailbox and (account or account_address))
    if (
        (provider == "imap" and not has_imap_identity)
        or (provider != "imap" and not (message_id or source_url or has_imap_identity))
    ):
        # Agent-produced metadata is display evidence, but it still needs a
        # stable identity anchor. A subject alone is too easy to spoof and
        # cannot safely address a later preview request.
        return None

    open_mode = "direct" if source_url else None
    if source_url is None and provider == "gmail" and message_id:
        # Himalaya exposes a mailbox-local UID plus the globally meaningful
        # RFC Message-ID. Gmail's account-neutral search URL is safer than
        # pretending that the IMAP UID is a Gmail thread identifier.
        search_message_id = (
            message_id[1:-1]
            if message_id.startswith("<") and message_id.endswith(">")
            else message_id
        )
        source_url = "https://mail.google.com/mail/#search/" + quote(
            f"rfc822msgid:{search_message_id}", safe=""
        )
        open_mode = "search"
    return EmailReferenceCandidate(
        provider=provider,
        account_address=account_address,
        account=account,
        mailbox=mailbox,
        uid=uid,
        message_id=message_id,
        sender_name=sender_name,
        sender_address=sender_address,
        subject=subject,
        received_at=_received_at(
            value.get("receivedAt")
            or value.get("received_at")
            or value.get("sentAt")
            or value.get("sent_at")
            or value.get("date")
        ),
        snippet=snippet,
        body_text=body_text,
        source_url=source_url,
        open_mode=open_mode,
    )


def safe_email_open_url(provider: str, value: Any) -> str | None:
    """Accept only direct HTTPS mailbox URLs for the declared provider."""

    if not isinstance(value, str) or not 1 <= len(value) <= 2_048:
        return None
    if _CONTROL_CHARACTERS.search(value) or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if provider == "gmail":
        # Gmail source URLs from untrusted agent output are deliberately
        # ignored. Control constructs its sole Gmail target from an RFC ID.
        return None
    if provider == "outlook":
        if host not in {
            "outlook.live.com",
            "outlook.office.com",
            "outlook.office365.com",
        }:
            return None
        if parsed.path == "/owa/":
            if not _valid_outlook_read_web_link(parsed.query) or parsed.fragment:
                return None
            return urlunsplit(
                ("https", parsed.netloc, parsed.path, parsed.query, "")
            )
        # Microsoft Graph's documented message.webLink is an /owa/ URL. Do
        # not label broad /mail/, compose, settings or options routes as a
        # concrete message target.
        return None
    return None


def _valid_outlook_read_web_link(query: str) -> bool:
    pairs = parse_qsl(query, keep_blank_values=True)
    if len(pairs) not in {3, 4}:
        return False
    values: dict[str, str] = {}
    for key, value in pairs:
        normalized = key.casefold()
        if normalized in values or normalized not in {
            "itemid",
            "exvsurl",
            "ispopout",
            "viewmodel",
        }:
            return False
        values[normalized] = value
    return (
        bool(values.get("itemid"))
        and len(values["itemid"]) <= 1_024
        and values.get("exvsurl") == "1"
        and values.get("viewmodel") == "ReadMessageItem"
        and values.get("ispopout", "0") in {"0", "1"}
    )


def _single_line(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _SPACE.sub(" ", _CONTROL_CHARACTERS.sub("", value)).strip()
    return cleaned[:maximum] or None


def _body_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL_CHARACTERS.sub("", value).strip()
    return cleaned[:12_000] or None


def _message_id(value: Any) -> str | None:
    rendered = _single_line(value, 998)
    if not rendered or any(character.isspace() for character in rendered):
        return None
    if rendered.startswith("<") or rendered.endswith(">"):
        if not (rendered.startswith("<") and rendered.endswith(">")):
            return None
        inner = rendered[1:-1]
    else:
        inner = rendered
    if (
        inner.count("@") != 1
        or not all(inner.split("@", 1))
        or any(character in inner for character in "<>")
    ):
        return None
    return rendered


def _uid(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    rendered = str(value) if isinstance(value, (str, int)) else ""
    if not rendered.isdecimal():
        return None
    parsed = int(rendered)
    return str(parsed) if 1 <= parsed <= 4_294_967_295 else None


def _received_at(value: Any) -> str | None:
    rendered = _single_line(value, 100)
    if not rendered:
        return None
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.isoformat().replace("+00:00", "Z")
