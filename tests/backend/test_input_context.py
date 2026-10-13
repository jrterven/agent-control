from hermes_client.attachments import compose_attachment_prompt, project_attachment_prompt
from hermes_client.types import PromptAttachment
from hermes_control_api.input_context import (
    DIRECT_CHAT_CONTEXT, LIVE_PREFIX, LIVE_SEPARATOR, project_direct_chat_prompt, with_direct_chat_context,
)
from hermes_control_api.semantic_search import public_text
from hermes_control_api.services import SessionService

VOICE_REQUEST = LIVE_PREFIX + "Keep your identity." + LIVE_SEPARATOR + "User: Only answer when I say Jarvis."
VOICE_HISTORY = [{"role": "user", "content": VOICE_REQUEST}]


def test_text_after_voice_is_explicitly_addressed_without_changing_live_requests():
    prompt = "¿Qué es este sensor?"
    assert with_direct_chat_context(prompt, []) == prompt
    assert with_direct_chat_context(VOICE_REQUEST, VOICE_HISTORY) == VOICE_REQUEST
    wrapped = with_direct_chat_context(prompt, VOICE_HISTORY)
    assert wrapped == DIRECT_CHAT_CONTEXT + prompt
    assert "wake-word" in wrapped
    assert "Respect any explicit request for silence" in wrapped
    assert project_direct_chat_prompt(wrapped) == prompt
    # A continuing typed conversation keeps its channel even after voice rows
    # have fallen outside the retained history window.
    assert with_direct_chat_context("Continúa", [{"role": "user", "content": wrapped}]) == DIRECT_CHAT_CONTEXT + "Continúa"


def test_quoted_or_partial_channel_context_is_not_reclassified_or_hidden():
    for prompt in ("Quote:\n" + DIRECT_CHAT_CONTEXT + "Example", DIRECT_CHAT_CONTEXT[:80]):
        assert project_direct_chat_prompt(prompt) == prompt
    for history in ([{"role": "assistant", "content": VOICE_REQUEST}], [{"role": "user", "content": "Quote:\n" + VOICE_REQUEST}]):
        assert with_direct_chat_context("Hola", history) == "Hola"


def test_channel_context_stays_private_with_attachment_metadata_and_reconciliation():
    question = "¿Qué es este sensor?"
    wrapped = with_direct_chat_context(question, VOICE_HISTORY)
    attachment = PromptAttachment(kind="image", name="sensor.png", media_type="image/png", content=b"image")
    composed = compose_attachment_prompt(wrapped, [attachment], [])
    visible, attachments = project_attachment_prompt(composed)
    assert project_direct_chat_prompt(visible) == question
    assert attachments[0]["name"] == "sensor.png"
    assert public_text([{"role": "user", "content": composed}], "gateway", "jarvis") == "user: " + question
    native = composed + "\n\n[Image attached at: /private/images/sensor.png]\n[screenshot]"
    assert SessionService._prompt_digest(composed) == SessionService._prompt_digest(native)
    blank = compose_attachment_prompt(with_direct_chat_context("", VOICE_HISTORY), [attachment], [])
    assert project_direct_chat_prompt(project_attachment_prompt(blank)[0]) == ""
