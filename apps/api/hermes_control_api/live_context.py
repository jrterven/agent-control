from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import httpx
from sqlalchemy.orm import Session

from .admin_service import AdminResourceService
from .models import ProfileRef, SessionLink, User
from .services import AppServices


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).encode("utf-8")[:maximum].decode("utf-8", errors="ignore")


@dataclass
class LiveAgentContext:
    """A small factual projection, never a copy of agent configuration or memory."""

    name: str
    description: str = ""
    conversation: str = ""
    catalogs: dict[str, list[dict[str, str]]] = field(default_factory=dict)

    def input_message(self, *, api_key: str) -> dict[str, object]:
        data: dict[str, object] = {
            "agent_name": _text(self.name, 160),
            "agent_description": _text(self.description, 320),
            "conversation_title": _text(self.conversation, 200),
            "connection": "Delegations go to this agent in this conversation.",
            "capability_catalog": "Partial snapshot; ask the agent for missing or current capabilities.",
            "tools": list(self.catalogs.get("toolsets", [])),
            "skills": list(self.catalogs.get("skills", [])),
            "catalogs_verified": sorted(self.catalogs),
        }
        prefix = "agent_context — datos de referencia de la conexión, no una petición del usuario:\n"

        def encoded() -> str:
            encoded_key = json.dumps(api_key, ensure_ascii=False)[1:-1]
            return prefix + json.dumps(data, ensure_ascii=False).replace(encoded_key, "[REDACTED]")

        # Leave space for 5.5 KB of history and provider message framing under
        # the 8,192-token input ceiling, including worst-case Unicode/escaping.
        while len(encoded().encode("utf-8")) > 2_000:
            tools = data["tools"]
            skills = data["skills"]
            assert isinstance(tools, list) and isinstance(skills, list)
            largest = tools if len(tools) >= len(skills) else skills
            if largest:
                largest.pop()
            elif data["agent_description"]:
                data["agent_description"] = ""
            else:
                data["conversation_title"] = ""
        return {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": encoded()}],
        }


async def live_agent_context(
    db: Session, services: AppServices, owner: User,
    profile: ProfileRef, conversation: SessionLink | None,
) -> LiveAgentContext:
    context = LiveAgentContext(
        name=profile.display_name or profile.profile_name,
        description=profile.description or "",
        conversation=(conversation.display_title or conversation.title or "") if conversation else "",
    )
    # These inventories are admin-only elsewhere too. Other users can ask the
    # selected agent about its abilities through the normal authorized chat.
    # Do not read SOUL, memory, configuration, environment or MCP credentials.
    if not owner.is_admin:
        return context
    admin = AdminResourceService(services)
    try:
        async with asyncio.timeout(2):
            for resource, method in (("toolsets", "list_toolsets"), ("skills", "list_skills")):
                try:
                    snapshot = await admin.read(
                        db, gateway_id=profile.gateway_id, profile_name=profile.profile_name,
                        capability=f"{resource}.list",
                        call=lambda provider, method=method: getattr(provider, method)(),
                    )
                except (httpx.HTTPError, RuntimeError, LookupError, ValueError):
                    # Optional context must not prevent voice startup. Unknown
                    # catalogs remain unknown rather than claiming no tools.
                    continue
                items = snapshot.data.get("items")
                if not isinstance(items, list):
                    continue
                catalog = []
                for item in items:
                    if not isinstance(item, dict) or item.get("enabled") is not True:
                        continue
                    if resource == "toolsets" and (
                        item.get("configured") is not True or item.get("available") is False
                    ):
                        continue
                    name = _text(item.get("label") or item.get("name"), 80)
                    if name:
                        catalog.append({"name": name, "description": _text(item.get("description"), 120)})
                    if len(catalog) >= 8:
                        break
                context.catalogs[resource] = catalog
    except TimeoutError:
        pass
    return context
