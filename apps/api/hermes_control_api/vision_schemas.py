from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from .schemas import ApiModel

VisionModelId = Literal["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
VisionMode = Literal["on_demand", "continuous"]


class VisionPreferencesMutation(ApiModel):
    model_id: VisionModelId = "gpt-5.6-luna"
    interval_seconds: Literal[2, 5, 10] = 5


class VisionPreferences(VisionPreferencesMutation):
    configured: bool


class VisionIntentRequest(ApiModel):
    request_id: UUID
    text: str = Field(min_length=1, max_length=4000)
    recent_context: str | None = Field(default=None, max_length=4000)


class VisionIntentResult(ApiModel):
    intent: Literal["visual", "nonvisual", "unclear"]
    question: str = Field(max_length=1500)


class VisionAnalysisRequest(ApiModel):
    request_id: UUID
    activation_id: UUID
    mode: VisionMode
    captured_at: datetime
    image: str = Field(min_length=1, max_length=1_398_130, repr=False)
    previous_image: str | None = Field(default=None, max_length=1_398_130, repr=False)
    question: str | None = Field(default=None, max_length=1500)
    recent_context: str | None = Field(default=None, max_length=4000)

    @field_validator("captured_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Capture time must include a timezone")
        return value


class VisionFinding(ApiModel):
    summary: str = Field(min_length=1, max_length=2400)
    meaningful_change: bool = Field(strict=True)
    scene_reset: bool = Field(strict=True)
    uncertainties: list[str] = Field(max_length=6)

    @field_validator("uncertainties")
    @classmethod
    def bounded_uncertainties(cls, value: list[str]) -> list[str]:
        if any(len(item) > 300 for item in value):
            raise ValueError("Uncertainty is too long")
        return value


class VisionObservation(VisionFinding):
    id: str
    session_id: str
    activation_id: str
    captured_at: datetime
    created_at: datetime
    model_id: VisionModelId
    mode: VisionMode


class VisionAnalysisResult(ApiModel):
    observation: VisionObservation
    published: bool


class VisionObservationPage(ApiModel):
    items: list[VisionObservation]
    next_cursor: str | None = None
