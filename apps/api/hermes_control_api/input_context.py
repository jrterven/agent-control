"""Keep live-listening conventions local to voice when a chat is reused."""
from hermes_client.attachments import project_attachment_prompt

LIVE_PREFIX = "This is a live voice request in your current conversation. "
LIVE_SEPARATOR = "\n\nLive conversation:\n"
DIRECT_CHAT_CONTEXT = (
    "<!-- hermes-control-text-input-v1\n"
    "Input channel: direct chat. The user explicitly sent the message below to you, "
    "including any attachments. Respond to this request in the chat. Earlier voice-only "
    "listening or wake-word rules (such as staying silent unless your name is spoken) "
    "apply to live audio, not to this direct message. Preserve all other user preferences, "
    "instructions and permissions. Respect any explicit request for silence or response "
    "format in the current message. Do not repeat previously completed external actions.\n"
    "-->\n\n"
)


def is_live_request(prompt: str) -> bool:
    return prompt.startswith(LIVE_PREFIX) and LIVE_SEPARATOR in prompt


def with_direct_chat_context(prompt: str, history: list[dict]) -> str:
    # Older clients already use this voice envelope. It classifies presentation
    # only; it never grants permissions or changes the submitted user's text.
    if is_live_request(prompt):
        return prompt
    for item in history:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content", item.get("text"))
        if not isinstance(content, str):
            continue
        content, _ = project_attachment_prompt(content)
        if is_live_request(content) or content.startswith(DIRECT_CHAT_CONTEXT):
            return DIRECT_CHAT_CONTEXT + prompt
    return prompt


def project_direct_chat_prompt(prompt: str) -> str:
    if prompt == DIRECT_CHAT_CONTEXT.rstrip():
        return ""
    return prompt.removeprefix(DIRECT_CHAT_CONTEXT)
