"""Tool-free inference contract and typed, data-only panel projections."""
import asyncio
import json
from typing import Literal

from pydantic import Field, FiniteFloat, model_validator
from sqlalchemy import select
from .home import owned_resources
from .home_sources import HomeUnavailable, provider_for
from .schemas import ApiModel


class Metric(ApiModel):
    label: str = Field(max_length=100)
    value: str = Field(max_length=100)


class Point(ApiModel):
    label: str = Field(max_length=100)
    value: FiniteFloat


class PanelBlock(ApiModel):
    type: Literal['summary', 'list', 'metrics', 'table', 'chart']
    title: str = Field(default='', max_length=160)
    text: str | None = Field(default=None, max_length=4000)
    items: list[str] | None = Field(default=None, max_length=20)
    metrics: list[Metric] | None = Field(default=None, max_length=8)
    columns: list[str] | None = Field(default=None, max_length=8)
    rows: list[list[str]] | None = Field(default=None, max_length=20)
    points: list[Point] | None = Field(default=None, max_length=20)


class GeneratedPanel(ApiModel):
    id: str = Field(max_length=80)
    title: str = Field(max_length=100)
    blocks: list[PanelBlock] = Field(max_length=8)


class GeneratedSuggestion(ApiModel):
    source_key: str = Field(max_length=2200)
    title: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=1000)
    quote: str = Field(min_length=1, max_length=500)
    draft: str = Field(max_length=6000)
    priority: int = Field(ge=0, le=100)
    due_at: str | None = Field(default=None, max_length=80)


class InsightResult(ApiModel):
    panels: list[GeneratedPanel] = Field(default_factory=list, max_length=20)
    suggestions: list[GeneratedSuggestion] = Field(default_factory=list, max_length=5)

    @model_validator(mode='after')
    def unique_sources(self):
        if len({s.source_key for s in self.suggestions}) != len(self.suggestions):
            raise ValueError('Group follow-up actions for the same source into one card')
        return self


async def analyze(db, services, owner, profile_id, context, operation_id):
    resources = owned_resources(db, owner, services.settings.deployment_mode == 'cloud')
    profile = next((p for p in resources['profiles'] if p.id == profile_id), None)
    if profile is None:
        raise HomeUnavailable('HOME_AGENT_UNAVAILABLE')
    provider = await provider_for(db, services, profile)
    capability = await provider.capabilities()
    if 'insights.generate' not in capability.methods or not callable(getattr(provider, 'generate_insight', None)):
        raise HomeUnavailable('HOME_CONNECTOR_UPDATE_REQUIRED')
    # Fixed input budget, including multi-byte text and reused drafts. Preserve
    # every source identity and shorten only evidence excerpts when necessary.
    context = json.loads(json.dumps(context))
    for _ in range(6):
        if len(json.dumps(context).encode()) <= 60000:
            break
        for source in context.get('sources', []):
            source['text'] = source.get('text', '')[:len(source.get('text', '')) // 2]
        for previous in context.get('previous', []):
            previous['draft'] = previous.get('draft', '')[:len(previous.get('draft', '')) // 2]
    if len(json.dumps(context).encode()) > 64000:
        raise HomeUnavailable('HOME_INPUT_TOO_LARGE')
    result = await asyncio.wait_for(provider.generate_insight(context, operation_id=operation_id), 160)
    if not isinstance(result, dict) or result.get('model') != 'gpt-6-luna':
        raise HomeUnavailable('HOME_MODEL_UNAVAILABLE')
    if len(json.dumps(result).encode()) > 64000:
        raise HomeUnavailable('HOME_INVALID_OUTPUT')
    try:
        parsed = InsightResult.model_validate(result['result'])
    except (ValueError, KeyError, TypeError):
        raise HomeUnavailable('HOME_INVALID_OUTPUT') from None
    # Reject invented panel identities and duplicate projections.
    wanted = {p['id'] for p in context.get('panels', [])}
    if {p.id for p in parsed.panels} != wanted or len(parsed.panels) != len(wanted):
        raise HomeUnavailable('HOME_INVALID_OUTPUT')
    return parsed, {k: max(0, int(result.get(k, 0))) for k in ('inputTokens', 'outputTokens')}


def panel_payload(generated, sources):
    return {'blocks': [b.model_dump(by_alias=True, exclude_none=True) for b in generated.blocks],
        'sources': [{k: row[k] for k in ('title', 'url', 'sessionId') if row.get(k)} for row in sources]}
