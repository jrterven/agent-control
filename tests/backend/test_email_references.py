from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hermes_client import (
    EventNormalizer,
    compose_email_reference_prompt,
    email_reference_candidates,
    project_email_reference_prompt,
)
from hermes_control_api.api.routes import bind_owned_realtime_event
from hermes_control_api.email_reference_cache import (
    cache_references,
    cached_reference,
    purge_expired,
    reference_id as scoped_reference_id,
)
from hermes_control_api.models import EmailReferenceCache, SessionLink, User, utc_now
from hermes_control_api.realtime import persist_normalized_event
from hermes_control_api.security import hash_password
from hermes_control_api.services import SessionService

from .conftest import mutation_headers


def _gateway_id(client: TestClient) -> str:
    response = client.get("/api/v1/gateways")
    assert response.status_code == 200
    return response.json()[0]["id"]


def _create_session(client: TestClient, csrf: str, key: str) -> dict:
    response = client.post(
        "/api/v1/sessions",
        headers=mutation_headers(csrf, key),
        json={
            "gatewayId": _gateway_id(client),
            "profileName": "control-dev",
            "title": "Email cards",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _marker(payload: dict) -> str:
    return (
        "<!-- hermes-control-email-reference-v1 "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + " -->"
    )


def test_email_reference_prompt_is_private_and_marker_projection_is_bounded():
    submitted = compose_email_reference_prompt("¿Qué correo requiere atención?")
    assert "hermes-control-email-ui-instruction-v2" in submitted
    assert "copy it to bodyText" in submitted
    visible, no_references = project_email_reference_prompt(submitted)
    assert visible == "¿Qué correo requiere atención?"
    assert no_references == []

    content = "Respuesta visible\n\n" + _marker(
        {
            "provider": "gmail",
            "accountAddress": "owner@example.com",
            "account": "personal-account",
            "mailbox": "INBOX",
            "uid": "42",
            "messageId": "<notice.42@example.com>",
            "senderName": "Google Ads",
            "senderAddress": "noreply@ads.google.com",
            "subject": "Action required",
            "receivedAt": "2026-08-31T01:29:00Z",
            "snippet": "The account requires verification.",
            "bodyText": "A plain-text preview.",
        }
    )
    visible, references = project_email_reference_prompt(content)
    assert visible == "Respuesta visible"
    assert len(references) == 1
    reference = references[0]
    assert reference.uid == "42"
    assert reference.source_url == (
        "https://mail.google.com/mail/#search/"
        "rfc822msgid%3Anotice.42%40example.com"
    )
    assert reference.open_mode == "search"
    public = reference.public_view(open_url="/safe/open")
    assert public["openMode"] == "search"
    assert "account" not in public
    assert "mailbox" not in public
    assert "uid" not in public
    assert "messageId" not in public
    assert "bodyText" not in public

    truncated, truncated_references = project_email_reference_prompt(
        "Respuesta recuperable\n<!-- hermes-control-email-reference-v1 "
        '{"provider":"gmail","subject":"partial"'
    )
    assert truncated == "Respuesta recuperable"
    assert truncated_references == []
    assert "partial" not in truncated

    delimiter_body = "Visible\n" + _marker(
        {
            "provider": "gmail",
            "subject": "Delimiter safety",
            "messageId": "<delimiter@example.com>",
            "bodyText": "Untrusted email text contains --> but stays JSON data",
        }
    )
    delimiter_visible, delimiter_references = project_email_reference_prompt(
        delimiter_body
    )
    assert delimiter_visible == "Visible"
    assert delimiter_references[0].body_text.endswith("stays JSON data")

    legacy_visible, legacy_references = project_email_reference_prompt(
        "Pregunta visible\n"
        "<hermes-control-email-ui-instruction-v1>legacy private protocol"
        "</hermes-control-email-ui-instruction-v1>"
    )
    assert legacy_visible == "Pregunta visible"
    assert legacy_references == []


@pytest.mark.parametrize(
    "prompt",
    [
        "Revisa mis correos",
        "Check my inbox",
        "Ouvre ma boîte de réception",
        "Prüfe meinen Posteingang",
        "Veja minha caixa de entrada",
        "Revisa Hostinger con Himalaya",
    ],
)
def test_email_protocol_is_intent_scoped_and_periodically_reprimed(prompt):
    first = SessionService._prompt_with_email_protocol(prompt, [])
    assert "hermes-control-email-ui-instruction-v2" in first

    recent = [
        {"role": "user", "content": first},
        {"role": "assistant", "content": "Listo"},
    ]
    second = SessionService._prompt_with_email_protocol(prompt, recent)
    assert second == prompt

    legacy_recent = [{
        "role": "user",
        "content": (
            "Legacy\n<hermes-control-email-ui-instruction-v1>old"
            "</hermes-control-email-ui-instruction-v1>"
        ),
    }]
    upgraded = SessionService._prompt_with_email_protocol(prompt, legacy_recent)
    assert "hermes-control-email-ui-instruction-v2" in upgraded

    old = [{"role": "user", "content": first}]
    old.extend(
        {"role": "user", "content": f"Mensaje normal {index}"}
        for index in range(20)
    )
    reprimed = SessionService._prompt_with_email_protocol(prompt, old)
    assert "hermes-control-email-ui-instruction-v2" in reprimed
    visible, _ = project_email_reference_prompt(reprimed)
    assert visible == prompt

    unrelated = SessionService._prompt_with_email_protocol(
        "Ayúdame a preparar el informe trimestral", []
    )
    assert unrelated == "Ayúdame a preparar el informe trimestral"


def test_email_preview_body_is_bounded_to_twelve_thousand_characters():
    [candidate] = email_reference_candidates(
        {
            "provider": "gmail",
            "subject": "Bounded body",
            "messageId": "<bounded@example.com>",
            "bodyText": "x" * 20_000,
        }
    )
    assert candidate.body_text == "x" * 12_000


def test_email_preview_accepts_plain_text_body_returned_by_provider_adapter():
    [candidate] = email_reference_candidates(
        {
            "provider": "gmail",
            "subject": "Adapter body",
            "messageId": "<adapter-body@example.com>",
            "body": "Plain text returned by the provider adapter.",
        }
    )
    assert candidate.body_text == "Plain text returned by the provider adapter."


def test_email_candidate_rejects_unsafe_urls_uids_and_unbounded_identity_fields():
    assert email_reference_candidates(
        {
            "provider": "gmail",
            "subject": "Unsafe target",
            "uid": "../../etc/passwd",
            "messageId": "not an RFC message id",
            "sourceUrl": "https://attacker.invalid/steal",
            "senderAddress": "victim@example.com\r\nBcc: attacker@example.com",
        }
    ) == []

    [candidate] = email_reference_candidates(
        {
            "provider": "gmail",
            "subject": "Bounded unsafe fields",
            "uid": "../../etc/passwd",
            "messageId": "<valid-anchor@example.com>",
            "sourceUrl": "https://attacker.invalid/steal",
            "senderAddress": "victim@example.com\r\nBcc: attacker@example.com",
        }
    )
    assert candidate.uid is None
    assert candidate.message_id == "<valid-anchor@example.com>"
    assert candidate.source_url == (
        "https://mail.google.com/mail/#search/"
        "rfc822msgid%3Avalid-anchor%40example.com"
    )
    assert candidate.sender_address is None
    assert candidate.public_view().get("openUrl") is None

    assert email_reference_candidates(
        {
            "provider": "gmail",
            "subject": "Bad UID",
            "uid": 4_294_967_296,
            "messageId": "<bad-uid@example.com>",
        }
    )[0].uid is None
    assert email_reference_candidates(
        {
            "provider": "outlook",
            "subject": "Open redirect",
            "sourceUrl": (
                "https://outlook.office.com/mail/deeplink/read/id"
                "?redirect=https%3A%2F%2Fattacker.invalid"
            ),
            "messageId": "<open-redirect@example.com>",
        }
    )[0].source_url is None

    outlook_web_link = (
        "https://outlook.office365.com/owa/"
        "?ItemID=AAMkAGVmMDEz"
        "&exvsurl=1&viewmodel=ReadMessageItem&ispopout=0"
    )
    [outlook_direct] = email_reference_candidates(
        {
            "provider": "outlook",
            "subject": "Graph webLink",
            "sourceUrl": outlook_web_link,
        }
    )
    assert outlook_direct.source_url == outlook_web_link
    assert outlook_direct.open_mode == "direct"
    assert email_reference_candidates(
        {
            "provider": "outlook",
            "subject": "Invalid popout",
            "sourceUrl": outlook_web_link.removesuffix("0") + "2",
            "messageId": "<invalid-popout@example.com>",
        }
    )[0].source_url is None
    assert email_reference_candidates(
        {
            "provider": "outlook",
            "subject": "Not a read link",
            "sourceUrl": (
                "https://outlook.office365.com/owa/"
                "?ItemID=AAMkAGVmMDEz&exvsurl=1&viewmodel=ComposeItem"
            ),
            "messageId": "<compose-item@example.com>",
        }
    )[0].source_url is None
    assert email_reference_candidates(
        {
            "provider": "outlook",
            "subject": "Extra redirect",
            "sourceUrl": outlook_web_link + "&redirect=https://attacker.invalid",
            "messageId": "<extra-redirect@example.com>",
        }
    )[0].source_url is None

    [gmail_derived] = email_reference_candidates(
        {
            "provider": "gmail",
            "subject": "Gmail ignores source URL",
            "messageId": "<gmail-link@example.com>",
            "sourceUrl": "https://mail.google.com/mail/u/0/#inbox/abc123",
        }
    )
    assert gmail_derived.open_mode == "search"
    assert gmail_derived.source_url.endswith(
        "rfc822msgid%3Agmail-link%40example.com"
    )
    assert email_reference_candidates(
        {
            "provider": "gmail",
            "subject": "Lookalike",
            "sourceUrl": "https://mail.google.com.attacker.invalid/mail/#inbox/abc",
        }
    ) == []

    for unsafe_outlook_url in (
        "https://outlook.office.com/mail/0/deeplink/compose?to=attacker@example.com",
        "https://outlook.office.com/mail/0/options/mail/layout",
        "https://outlook.live.com/mail/0/inbox/id/AAQk123",
    ):
        [blocked] = email_reference_candidates(
            {
                "provider": "outlook",
                "subject": "Blocked non-Graph route",
                "messageId": "<blocked-outlook@example.com>",
                "sourceUrl": unsafe_outlook_url,
            }
        )
        assert blocked.source_url is None


def test_normalizer_projects_structured_tool_email_without_private_fields():
    event = EventNormalizer(gateway_id="g1", profile_name="jarvis").normalize(
        {
            "event": "tool.completed",
            "payload": {
                "name": "himalaya.read",
                "status": "completed",
                "emailReferences": [
                    {
                        "provider": "gmail",
                        "accountAddress": "owner@example.com",
                        "mailbox": "INBOX",
                        "uid": 91,
                        "messageId": "<message.91@example.com>",
                        "subject": "Tool result",
                        "bodyText": "PRIVATE BODY",
                    }
                ],
            },
        }
    )
    references = event.data["controlEmailReferences"]
    assert references[0]["provider"] == "gmail"
    rendered = json.dumps(event.to_dict())
    assert "PRIVATE BODY" not in rendered
    assert "owner@example.com" not in rendered
    assert "message.91@example.com" not in rendered
    assert "mailbox" not in rendered
    assert "uid" not in rendered


def test_generic_himalaya_reference_projects_as_preview_only_imap():
    [candidate] = email_reference_candidates(
        {
            "provider": "hostinger",
            "account": "juan-jemailabs",
            "mailbox": "INBOX",
            "uid": 314,
            "messageId": "<hostinger-message@example.com>",
            "subject": "Hostinger IMAP result",
            "bodyText": "Plain text returned by Himalaya",
            "sourceUrl": "https://webmail.hostinger.com/arbitrary",
        }
    )
    assert candidate.provider == "imap"
    assert candidate.source_url is None
    assert candidate.open_mode is None
    assert "openUrl" not in candidate.public_view(open_url=None)
    assert email_reference_candidates(
        {
            "provider": "imap",
            "subject": "Missing mailbox identity",
            "messageId": "<message-id-alone-is-not-enough@example.com>",
        }
    ) == []


def test_himalaya_consumer_accounts_recover_provider_affordances():
    [gmail] = email_reference_candidates(
        {
            "provider": "himalaya",
            "accountAddress": "owner@gmail.com",
            "account": "personal",
            "mailbox": "INBOX",
            "uid": 315,
            "messageId": "<gmail-over-imap@example.com>",
            "subject": "Gmail over IMAP",
        }
    )
    assert gmail.provider == "gmail"
    assert gmail.open_mode == "search"
    assert gmail.source_url.endswith(
        "rfc822msgid%3Agmail-over-imap%40example.com"
    )

    [outlook] = email_reference_candidates(
        {
            "provider": "imap",
            "accountAddress": "owner@outlook.com",
            "account": "work",
            "mailbox": "INBOX",
            "uid": 316,
            "messageId": "<outlook-over-imap@example.com>",
            "subject": "Outlook over IMAP",
        }
    )
    assert outlook.provider == "outlook"
    assert outlook.source_url is None
    assert outlook.open_mode is None


def test_every_split_email_marker_boundary_is_statefully_private():
    marker = _marker(
        {
            "provider": "gmail",
            "accountAddress": "split-owner@example.com",
            "account": "SPLIT-PRIVATE-ACCOUNT",
            "mailbox": "SPLIT-PRIVATE-MAILBOX",
            "uid": 4242,
            "messageId": "<split-private-message@example.com>",
            "senderName": "Visible sender",
            "senderAddress": "sender@example.com",
            "subject": "Visible subject",
            "snippet": "Visible snippet",
            "bodyText": "SPLIT-PRIVATE-BODY",
        }
    )
    private_values = (
        "split-owner@example.com",
        "SPLIT-PRIVATE-ACCOUNT",
        "SPLIT-PRIVATE-MAILBOX",
        "split-private-message@example.com",
        "SPLIT-PRIVATE-BODY",
        "hermes-control-email-reference-v1",
    )

    for split in range(1, len(marker)):
        normalizer = EventNormalizer(gateway_id="g1", profile_name="jarvis")
        projected = [
            normalizer.normalize(
                {
                    "event": "message.delta",
                    "payload": {
                        "session_id": "runtime-split",
                        "message_id": "message-split",
                        "delta": "Visible answer" + marker[:split],
                    },
                }
            ),
            normalizer.normalize(
                {
                    "event": "message.delta",
                    "payload": {
                        "session_id": "runtime-split",
                        "message_id": "message-split",
                        "delta": marker[split:],
                    },
                }
            ),
        ]
        rendered_events = json.dumps([event.to_dict() for event in projected])
        for private in private_values:
            assert private not in rendered_events, (split, private)
        assert '"uid"' not in rendered_events
        assert "".join(str(event.data.get("delta") or "") for event in projected) == (
            "Visible answer"
        )
        references = [
            reference
            for event in projected
            for reference in event.data.get("controlEmailReferences", [])
        ]
        assert len(references) == 1
        assert references[0]["subject"] == "Visible subject"


def test_marker_casing_and_spacing_variants_are_suppressed_consistently():
    variant = (
        '<!--   HERMES-CONTROL-EMAIL-REFERENCE-V1 {"provider":"gmail",'
        '"subject":"Variant subject","messageId":"<variant@example.com>",'
        '"bodyText":"VARIANT-PRIVATE-BODY"} -->'
    )
    visible, references = project_email_reference_prompt("Visible" + variant)
    assert visible == "Visible"
    assert references[0].subject == "Variant subject"

    normalizer = EventNormalizer(gateway_id="g1", profile_name="jarvis")
    chunks = ("Visible" + variant[:7], variant[7:35], variant[35:])
    events = [
        normalizer.normalize(
            {
                "event": "message.delta",
                "payload": {
                    "session_id": "runtime-variant",
                    "delta": chunk,
                },
            }
        )
        for chunk in chunks
    ]
    rendered = json.dumps([event.to_dict() for event in events])
    assert "VARIANT-PRIVATE-BODY" not in rendered
    assert "HERMES-CONTROL-EMAIL-REFERENCE-V1" not in rendered
    assert "".join(str(event.data.get("delta") or "") for event in events) == (
        "Visible"
    )
    assert sum(
        len(event.data.get("controlEmailReferences", [])) for event in events
    ) == 1


def test_bytewise_email_marker_and_truncated_terminal_never_leak_private_payload():
    marker = _marker(
        {
            "provider": "gmail",
            "subject": "Bytewise subject",
            "messageId": "<bytewise@example.com>",
            "account": "BYTEWISE-PRIVATE-ACCOUNT",
            "bodyText": "BYTEWISE-PRIVATE-BODY",
        }
    )
    normalizer = EventNormalizer(gateway_id="g1", profile_name="jarvis")
    events = []
    for index, character in enumerate("Visible bytewise" + marker):
        events.append(
            normalizer.normalize(
                {
                    "event": "message.delta",
                    "payload": {
                        "session_id": "runtime-bytewise",
                        "message_id": "message-bytewise",
                        "seq": index,
                        "delta": character,
                    },
                }
            )
        )
    rendered = json.dumps([event.to_dict() for event in events])
    assert "BYTEWISE-PRIVATE-ACCOUNT" not in rendered
    assert "BYTEWISE-PRIVATE-BODY" not in rendered
    assert "hermes-control-email-reference-v1" not in rendered
    assert "".join(str(event.data.get("delta") or "") for event in events) == (
        "Visible bytewise"
    )
    assert sum(
        len(event.data.get("controlEmailReferences", [])) for event in events
    ) == 1

    truncated = EventNormalizer(gateway_id="g1", profile_name="jarvis")
    first = truncated.normalize(
        {
            "event": "message.delta",
            "payload": {
                "session_id": "runtime-truncated",
                "delta": "Safe answer" + marker[:-8],
            },
        }
    )
    terminal = truncated.normalize(
        {
            "event": "message.complete",
            "payload": {"session_id": "runtime-truncated", "status": "completed"},
        }
    )
    truncated_rendered = json.dumps([first.to_dict(), terminal.to_dict()])
    assert "Safe answer" in truncated_rendered
    assert "BYTEWISE-PRIVATE-ACCOUNT" not in truncated_rendered
    assert "BYTEWISE-PRIVATE-BODY" not in truncated_rendered
    assert "hermes-control-email-reference-v1" not in truncated_rendered


@pytest.mark.parametrize("terminal_field", ["content", "text"])
def test_private_marker_tail_can_move_from_delta_to_terminal_field(terminal_field):
    marker = _marker(
        {
            "provider": "gmail",
            "subject": "Cross-event subject",
            "account": "CROSS-EVENT-PRIVATE-ACCOUNT",
            "mailbox": "CROSS-EVENT-PRIVATE-MAILBOX",
            "uid": 77,
            "messageId": "<cross-event-private@example.com>",
            "bodyText": "CROSS-EVENT-PRIVATE-BODY",
        }
    )
    split = len(marker) // 2
    normalizer = EventNormalizer(gateway_id="g1", profile_name="jarvis")
    delta = normalizer.normalize(
        {
            "event": "message.delta",
            "payload": {
                "session_id": "runtime-cross-event",
                "message_id": "delta-message-id",
                "delta": "Visible terminal bridge" + marker[:split],
            },
        }
    )
    terminal = normalizer.normalize(
        {
            "event": "message.complete",
            "payload": {
                "session_id": "runtime-cross-event",
                # Official gateways do not promise the same message id here.
                "message_id": "terminal-message-id",
                terminal_field: marker[split:],
                "status": "completed",
            },
        }
    )
    rendered = json.dumps([delta.to_dict(), terminal.to_dict()])
    assert delta.data["delta"] == "Visible terminal bridge"
    assert terminal.data[terminal_field] == ""
    assert terminal.data["controlEmailReferences"][0]["subject"] == (
        "Cross-event subject"
    )
    for private in (
        "CROSS-EVENT-PRIVATE-ACCOUNT",
        "CROSS-EVENT-PRIVATE-MAILBOX",
        "cross-event-private@example.com",
        "CROSS-EVENT-PRIVATE-BODY",
        "hermes-control-email-reference-v1",
    ):
        assert private not in rendered


def test_cumulative_terminal_after_partial_marker_is_quarantined():
    marker = _marker(
        {
            "provider": "gmail",
            "subject": "Cumulative subject",
            "bodyText": "CUMULATIVE-PRIVATE-BODY",
        }
    )
    normalizer = EventNormalizer(gateway_id="g1", profile_name="jarvis")
    first = normalizer.normalize(
        {
            "event": "message.delta",
            "payload": {
                "session_id": "runtime-cumulative",
                "delta": "Visible cumulative" + marker[: len(marker) // 2],
            },
        }
    )
    # Some providers repeat the complete accumulated message rather than just
    # the tail. It is acceptable to recover the card from durable history; the
    # realtime boundary must prefer omission over exposing transport metadata.
    terminal = normalizer.normalize(
        {
            "event": "message.complete",
            "payload": {
                "session_id": "runtime-cumulative",
                "content": "Visible cumulative" + marker,
                "status": "completed",
            },
        }
    )
    rendered = json.dumps([first.to_dict(), terminal.to_dict()])
    assert first.data["delta"] == "Visible cumulative"
    assert terminal.data["content"] == ""
    assert "CUMULATIVE-PRIVATE-BODY" not in rendered
    assert "hermes-control-email-reference-v1" not in rendered


def test_oversized_stream_marker_tombstones_route_until_terminal():
    normalizer = EventNormalizer(gateway_id="g1", profile_name="jarvis")
    first = normalizer.normalize(
        {
            "event": "message.delta",
            "payload": {
                "session_id": "runtime-overflow",
                "delta": (
                    "Visible before overflow"
                    '<!-- hermes-control-email-reference-v1 {"provider":"gmail",'
                    '"subject":"Overflow","bodyText":"'
                    + ("O" * (73 * 1024))
                ),
            },
        }
    )
    tail = normalizer.normalize(
        {
            "event": "message.delta",
            "payload": {
                "session_id": "runtime-overflow",
                "delta": 'OVERFLOW-PRIVATE-TAIL"} -->must stay quarantined',
            },
        }
    )
    assert first.data["delta"] == "Visible before overflow"
    assert tail.data["delta"] == ""
    rendered = json.dumps([first.to_dict(), tail.to_dict()])
    assert "OVERFLOW-PRIVATE-TAIL" not in rendered
    assert "hermes-control-email-reference-v1" not in rendered

    normalizer.normalize(
        {
            "event": "message.complete",
            "payload": {"session_id": "runtime-overflow", "status": "completed"},
        }
    )
    next_turn = normalizer.normalize(
        {
            "event": "message.delta",
            "payload": {
                "session_id": "runtime-overflow",
                "delta": "Visible after terminal",
            },
        }
    )
    assert next_turn.data["delta"] == "Visible after terminal"


def test_stream_cardinality_uses_tombstones_then_fails_closed_at_capacity():
    normalizer = EventNormalizer(gateway_id="g1", profile_name="jarvis")
    for index in range(577):
        event = normalizer.normalize(
            {
                "event": "message.delta",
                "payload": {
                    "session_id": f"runtime-cardinality-{index}",
                    "delta": (
                        f"Visible {index}"
                        '<!-- hermes-control-email-reference-v1 {"provider"'
                    ),
                },
            }
        )
        assert "hermes-control-email-reference-v1" not in json.dumps(
            event.to_dict()
        )

    private_tail = normalizer.normalize(
        {
            "event": "message.delta",
            "payload": {
                "session_id": "runtime-cardinality-0",
                "delta": ':"gmail","subject":"CARDINALITY-PRIVATE-TAIL"} -->',
            },
        }
    )
    unrelated = normalizer.normalize(
        {
            "event": "message.delta",
            "payload": {
                "session_id": "runtime-after-cardinality",
                "delta": "Generation stays fail-closed",
            },
        }
    )
    assert private_tail.data["delta"] == ""
    assert unrelated.data["delta"] == ""
    rendered = json.dumps([private_tail.to_dict(), unrelated.to_dict()])
    assert "CARDINALITY-PRIVATE-TAIL" not in rendered


def test_history_email_preview_and_redirect_are_owned_and_provider_allowlisted(
    authenticated, app
):
    client, csrf = authenticated
    session = _create_session(client, csrf, "email-reference-session")
    payload = {
        "provider": "gmail",
        "accountAddress": "owner@example.com",
        "account": "private-profile-name",
        "mailbox": "INBOX",
        "uid": 123,
        "messageId": "<urgent.123@example.com>",
        "senderName": "Google Ads",
        "senderAddress": "noreply@ads.google.com",
        "subject": "[Action required] Your account will be paused",
        "receivedAt": "2026-08-31T01:29:00Z",
        "snippet": "Verification is required within ten days.",
        "bodyText": "Start advertiser verification now.\nCustomer ID: 867-796-4827",
    }

    async def seed_history() -> None:
        with app.state.session_factory() as db:
            row = db.get(SessionLink, session["id"])
            from hermes_control_api.services import GatewayService

            connection = await GatewayService(app.state.services).connection(
                db, row.gateway_id, row.profile_name
            )
        provider = await app.state.services.provider_pool.get(connection)
        provider._messages[row.stored_session_id] = [
            {
                "id": "user-email-question",
                "role": "user",
                "content": compose_email_reference_prompt("¿Hay algo urgente?"),
            },
            {
                "id": "assistant-email-answer",
                "role": "assistant",
                "content": "Sí, este correo requiere atención.\n\n" + _marker(payload),
            },
        ]

    client.portal.call(seed_history)
    history = client.get(f"/api/v1/sessions/{session['id']}/messages")
    assert history.status_code == 200, history.text
    user_message, assistant_message = history.json()["items"]
    assert user_message["content"] == "¿Hay algo urgente?"
    assert "hermes-control-email" not in history.text
    assert assistant_message["content"] == "Sí, este correo requiere atención."
    [reference] = assistant_message["controlEmailReferences"]
    assert reference["schemaVersion"] == 1
    assert reference["openMode"] == "search"
    assert reference["previewUrl"].startswith("/api/v1/sessions/")
    assert reference["openUrl"].endswith("/open")
    assert "bodyText" not in reference
    assert "private-profile-name" not in history.text
    assert "urgent.123@example.com" not in history.text

    preview = client.get(reference["previewUrl"])
    assert preview.status_code == 200, preview.text
    assert "no-store" in preview.headers["cache-control"]
    assert "object-src 'none'" in preview.headers["content-security-policy"]
    assert "base-uri 'none'" in preview.headers["content-security-policy"]
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert preview.json()["bodyText"].startswith("Start advertiser verification")
    assert preview.json()["openMode"] == "search"
    assert "account" not in preview.json()
    assert "mailbox" not in preview.json()
    assert "uid" not in preview.json()
    assert "messageId" not in preview.json()

    opened = client.get(reference["openUrl"], follow_redirects=False)
    assert opened.status_code == 307
    assert opened.headers["referrer-policy"] == "no-referrer"
    assert opened.headers["location"] == (
        "https://mail.google.com/mail/#search/"
        "rfc822msgid%3Aurgent.123%40example.com"
    )

    missing = client.get(
        f"/api/v1/sessions/{session['id']}/email-references/{'0' * 32}"
    )
    assert missing.status_code == 404

    with app.state.session_factory() as db:
        db.add(
            User(
                username="email-reference-outsider",
                password_hash=hash_password("another correct horse battery staple"),
            )
        )
        db.commit()
    login = client.post(
        "/api/v1/auth/login",
        json={
            "username": "email-reference-outsider",
            "password": "another correct horse battery staple",
        },
    )
    assert login.status_code == 200
    assert client.get(reference["previewUrl"]).status_code == 404
    assert client.get(reference["openUrl"], follow_redirects=False).status_code == 404


def test_history_projects_structured_tool_reference_without_parsing_tool_content(
    authenticated, app
):
    client, csrf = authenticated
    session = _create_session(client, csrf, "structured-tool-email-reference")

    async def seed_history() -> None:
        with app.state.session_factory() as db:
            row = db.get(SessionLink, session["id"])
            from hermes_control_api.services import GatewayService

            connection = await GatewayService(app.state.services).connection(
                db, row.gateway_id, row.profile_name
            )
        provider = await app.state.services.provider_pool.get(connection)
        provider._messages[row.stored_session_id] = [
            {
                "id": "tool-email",
                "role": "tool",
                "content": _marker(
                    {"provider": "gmail", "subject": "Must not parse content"}
                ),
                "email_references": [
                    {
                        "provider": "outlook",
                        "subject": "Structured reference",
                        "messageId": "<structured@example.com>",
                        "senderAddress": "sender@example.com",
                        "bodyText": "Structured body",
                    }
                ],
            },
            {
                "id": "assistant-after-tool",
                "role": "assistant",
                "content": "Encontré este correo.",
            },
        ]

    client.portal.call(seed_history)
    history = client.get(f"/api/v1/sessions/{session['id']}/messages")
    assert history.status_code == 200, history.text
    tool_message = history.json()["items"][0]
    [reference] = tool_message["controlEmailReferences"]
    assert reference["provider"] == "outlook"
    assert reference["subject"] == "Structured reference"
    assert "openUrl" not in reference
    assert tool_message["content"] == ""
    assert "Must not parse content" not in history.text
    assert "hermes-control-email-reference" not in history.text
    assert "Structured body" not in history.text


def test_scoped_ids_cover_body_and_session_and_realtime_binder_is_public_only(
    authenticated, app
):
    client, csrf = authenticated
    first_session = _create_session(client, csrf, "email-hmac-first")
    second_session = _create_session(client, csrf, "email-hmac-second")
    [first] = email_reference_candidates(
        {
            "provider": "gmail",
            "subject": "Same message, first body",
            "messageId": "<opaque-id@example.com>",
            "bodyText": "FIRST PRIVATE BODY",
        }
    )
    [changed_body] = email_reference_candidates(
        {
            "provider": "gmail",
            "subject": "Same message, first body",
            "messageId": "<opaque-id@example.com>",
            "bodyText": "SECOND PRIVATE BODY",
        }
    )
    with app.state.session_factory() as db:
        first_row = db.get(SessionLink, first_session["id"])
        second_row = db.get(SessionLink, second_session["id"])
        owner_id = first_row.owner_id
        first_id = scoped_reference_id(app.state.services.vault, first_row, first)
        changed_id = scoped_reference_id(
            app.state.services.vault, first_row, changed_body
        )
        other_session_id = scoped_reference_id(
            app.state.services.vault, second_row, first
        )
    assert len({first_id, changed_id, other_session_id}) == 3

    transport = {
        **first.transport_view(),
        "bodyText": "MUST NOT CROSS",
        "account": "PRIVATE ACCOUNT",
        "mailbox": "PRIVATE MAILBOX",
        "uid": "99",
        "messageId": "<must-not-cross@example.com>",
        "sourceUrl": "https://attacker.invalid/",
        "openUrl": "https://attacker.invalid/",
    }
    bound = bind_owned_realtime_event(
        app.state.session_factory,
        user_id=owner_id,
        email_reference_key=app.state.services.vault.key,
        payload={
            "type": "message.delta",
            "gatewayId": first_session["gatewayId"],
            "profileName": first_session["profileName"],
            "storedSessionId": first_session["storedSessionId"],
            "data": {"delta": "Visible", "controlEmailReferences": [transport]},
        },
    )
    assert bound is not None
    [public] = bound["data"]["controlEmailReferences"]
    assert public["id"] == first_id
    assert public["previewUrl"].endswith(first_id)
    rendered = json.dumps(bound)
    for private in (
        "_fingerprint",
        "FIRST PRIVATE BODY",
        "MUST NOT CROSS",
        "PRIVATE ACCOUNT",
        "PRIVATE MAILBOX",
        "must-not-cross@example.com",
        "attacker.invalid",
    ):
        assert private not in rendered


def test_durable_event_caches_email_without_browser_and_preview_survives_offline(
    authenticated, app, monkeypatch
):
    client, csrf = authenticated
    session = _create_session(client, csrf, "durable-email-no-browser")
    marker = _marker(
        {
            "provider": "gmail",
            "subject": "Background result",
            "messageId": "<background@example.com>",
            "senderAddress": "sender@example.com",
            "bodyText": "BACKGROUND PRIVATE BODY",
        }
    )
    normalizer = EventNormalizer(
        gateway_id=session["gatewayId"],
        profile_name=session["profileName"],
    )
    event = normalizer.normalize(
        {
            "event": "message.delta",
            "payload": {
                "session_key": session["storedSessionId"],
                "delta": "Finished in background" + marker,
            },
        }
    )
    # The public/event-hub representation never contains the private cache seed.
    assert "BACKGROUND PRIVATE BODY" not in json.dumps(event.to_dict())
    persist_normalized_event(
        app.state.session_factory,
        event,
        vault=app.state.services.vault,
    )
    [candidate] = email_reference_candidates(
        {
            "provider": "gmail",
            "subject": "Background result",
            "messageId": "<background@example.com>",
            "senderAddress": "sender@example.com",
            "bodyText": "BACKGROUND PRIVATE BODY",
        }
    )
    with app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        opaque_id = scoped_reference_id(app.state.services.vault, row, candidate)
        cached = db.scalar(
            select(EmailReferenceCache).where(
                EmailReferenceCache.reference_id == opaque_id
            )
        )
        assert cached is not None
        assert "BACKGROUND PRIVATE BODY" not in cached.payload_ciphertext
        assert "mail.google.com" not in cached.payload_ciphertext

    async def gateway_is_asleep(*_args, **_kwargs):
        raise AssertionError("cache-first preview contacted the sleeping gateway")

    monkeypatch.setattr(SessionService, "_raw_history", gateway_is_asleep)
    preview_url = (
        f"/api/v1/sessions/{session['id']}/email-references/{opaque_id}"
    )
    preview = client.get(preview_url)
    assert preview.status_code == 200, preview.text
    assert preview.json()["bodyText"] == "BACKGROUND PRIVATE BODY"
    opened = client.get(preview_url + "/open", follow_redirects=False)
    assert opened.status_code == 307
    assert "rfc822msgid%3Abackground%40example.com" in opened.headers["location"]


def test_email_cache_is_fixed_ttl_tamper_evident_owner_scoped_and_bounded(
    authenticated, app
):
    client, csrf = authenticated
    first = _create_session(client, csrf, "email-cache-security-first")
    second = _create_session(client, csrf, "email-cache-security-second")
    # ``email_reference_candidates`` has a transport cardinality limit, so
    # build the cache load as validated singletons in newest-first order.
    candidates = [
        email_reference_candidates(
            {
                "provider": "gmail",
                "subject": f"Reference {index}",
                "messageId": f"<cache-{index}@example.com>",
                "bodyText": f"PRIVATE-CACHE-{index}",
            }
        )[0]
        for index in range(512, -1, -1)
    ]
    with app.state.session_factory() as db:
        first_row = db.get(SessionLink, first["id"])
        second_row = db.get(SessionLink, second["id"])
        cache_references(
            db,
            app.state.services.vault,
            first_row,
            candidates,
            candidate_limit=513,
        )
        db.commit()
        newest_id = scoped_reference_id(
            app.state.services.vault, first_row, candidates[0]
        )
        oldest_id = scoped_reference_id(
            app.state.services.vault, first_row, candidates[-1]
        )
        rows = list(
            db.scalars(
                select(EmailReferenceCache).where(
                    EmailReferenceCache.session_link_id == first_row.id
                )
            ).all()
        )
        assert len(rows) == 512
        assert any(row.reference_id == newest_id for row in rows)
        assert not any(row.reference_id == oldest_id for row in rows)

        newest = next(row for row in rows if row.reference_id == newest_id)
        original_ciphertext = newest.payload_ciphertext
        original_expiry = newest.expires_at
        cache_references(
            db,
            app.state.services.vault,
            first_row,
            [candidates[0]],
        )
        db.commit()
        db.refresh(newest)
        assert newest.payload_ciphertext == original_ciphertext
        assert newest.expires_at == original_expiry

        # Copying ciphertext to another session changes both AAD and HMAC scope.
        copied_id = scoped_reference_id(
            app.state.services.vault, second_row, candidates[0]
        )
        copied = EmailReferenceCache(
            owner_id=second_row.owner_id,
            session_link_id=second_row.id,
            reference_id=copied_id,
            payload_ciphertext=original_ciphertext,
            expires_at=utc_now() + timedelta(days=7),
        )
        db.add(copied)
        db.commit()
        assert cached_reference(
            db,
            app.state.services.vault,
            second_row,
            copied_id,
        ) is None
        db.rollback()

        newest.payload_ciphertext = "v1.invalid.invalid"
        db.commit()
        assert cached_reference(
            db,
            app.state.services.vault,
            first_row,
            newest_id,
        ) is None
        db.commit()

        expiring = db.scalar(
            select(EmailReferenceCache).where(
                EmailReferenceCache.session_link_id == first_row.id
            )
        )
        expiring.expires_at = utc_now() - timedelta(seconds=1)
        expired_id = expiring.id
        db.commit()
        assert purge_expired(db) >= 1
        db.commit()
        assert db.get(EmailReferenceCache, expired_id) is None

        cache_references(
            db,
            app.state.services.vault,
            second_row,
            [candidates[1]],
        )
        db.commit()
        assert db.scalar(
            select(EmailReferenceCache).where(
                EmailReferenceCache.session_link_id == second_row.id
            )
        ) is not None
        second_id = second_row.id
        db.delete(second_row)
        db.commit()
        assert db.scalar(
            select(EmailReferenceCache).where(
                EmailReferenceCache.session_link_id == second_id
            )
        ) is None


def test_live_history_resolves_actionable_reference_beyond_cache_window(
    authenticated, app
):
    client, csrf = authenticated
    session = _create_session(client, csrf, "email-reference-257")

    async def seed_long_history() -> None:
        with app.state.session_factory() as db:
            row = db.get(SessionLink, session["id"])
            from hermes_control_api.services import GatewayService

            connection = await GatewayService(app.state.services).connection(
                db, row.gateway_id, row.profile_name
            )
        provider = await app.state.services.provider_pool.get(connection)
        provider._messages[row.stored_session_id] = [
            {
                "id": f"assistant-{index}",
                "role": "assistant",
                "content": "Result "
                + _marker(
                    {
                        "provider": "gmail",
                        "subject": f"Mail {index}",
                        "messageId": f"<long-history-{index}@example.com>",
                        "bodyText": f"Long history body {index}",
                    }
                ),
            }
            for index in range(257)
        ]

    client.portal.call(seed_long_history)
    history = client.get(f"/api/v1/sessions/{session['id']}/messages")
    assert history.status_code == 200, history.text
    [oldest] = history.json()["items"][0]["controlEmailReferences"]
    preview = client.get(oldest["previewUrl"])
    assert preview.status_code == 200, preview.text
    assert preview.json()["bodyText"] == "Long history body 0"


def test_history_only_projects_the_newest_cacheable_reference_window(
    authenticated, app
):
    client, csrf = authenticated
    session = _create_session(client, csrf, "email-reference-window")

    async def seed_overflow_history() -> None:
        with app.state.session_factory() as db:
            row = db.get(SessionLink, session["id"])
            from hermes_control_api.services import GatewayService

            connection = await GatewayService(app.state.services).connection(
                db, row.gateway_id, row.profile_name
            )
        provider = await app.state.services.provider_pool.get(connection)
        provider._messages[row.stored_session_id] = [
            {
                "id": f"assistant-window-{index}",
                "role": "assistant",
                "content": "Result "
                + "".join(
                    _marker(
                        {
                            "provider": "gmail",
                            "subject": f"Window mail {index}-{card}",
                            "messageId": f"<window-{index}-{card}@example.com>",
                            "bodyText": f"Window body {index}-{card}",
                        }
                    )
                    for card in range(8)
                ),
            }
            for index in range(65)
        ]

    client.portal.call(seed_overflow_history)
    history = client.get(f"/api/v1/sessions/{session['id']}/messages")
    assert history.status_code == 200, history.text
    items = history.json()["items"]
    assert "controlEmailReferences" not in items[0]
    assert items[-1]["controlEmailReferences"][-1]["subject"] == "Window mail 64-7"
    assert sum(
        len(item.get("controlEmailReferences", [])) for item in items
    ) == 512
    with app.state.session_factory() as db:
        assert len(
            list(
                db.scalars(
                    select(EmailReferenceCache).where(
                        EmailReferenceCache.session_link_id == session["id"]
                    )
                ).all()
            )
        ) == 512
