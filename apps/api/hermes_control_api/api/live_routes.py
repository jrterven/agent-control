from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user, get_db, require_csrf, require_idempotency
from ..integration_schemas import (
    LiveSessionRequest,
    LiveSessionView,
    LiveVoicePreviewRequest,
    OpenAIIntegrationView,
    OpenAIKeyMutation,
    OpenAIProfileVoiceView,
    OpenAIVoiceSettingsView,
    VoiceSettingsView,
)
from ..integrations import IntegrationError
from ..live_context import live_agent_context
from ..models import AuthSession, Gateway, OpenAIProfileVoicePreference, ProfileRef, SessionLink, User
from ..openai_live import (
    OpenAIIntegrationService,
    openai_voice_id,
    set_openai_voice_id,
    set_profile_openai_voice_id,
    set_voice_provider,
    voice_provider,
)
from ..services import (
    NotFoundError, SessionService, audit, require_capability, require_mutable_profile,
)


router = APIRouter(prefix="/api/v1", tags=["live voice"])


def _service(request: Request) -> OpenAIIntegrationService:
    return OpenAIIntegrationService(request.app.state.services.vault)


def _audit(
    db: Session,
    request: Request,
    owner: User,
    action: str,
    *,
    failed: bool = False,
) -> None:
    audit(
        db,
        actor=owner,
        action=action,
        target_type="integration",
        target_id="openai",
        outcome="failure" if failed else "success",
        request_id=getattr(request.state, "request_id", None),
    )


@router.get("/integrations/voice", response_model=VoiceSettingsView)
def voice_settings(
    owner: User = Depends(current_user), db: Session = Depends(get_db)
) -> VoiceSettingsView:
    return VoiceSettingsView(provider=voice_provider(db, owner))


@router.put("/integrations/voice", response_model=VoiceSettingsView)
def update_voice_settings(
    payload: VoiceSettingsView,
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    _: str = Depends(require_idempotency),
    db: Session = Depends(get_db),
) -> VoiceSettingsView:
    if payload.provider == "openai_live":
        _service(request).api_key(db, auth.user)
    set_voice_provider(db, auth.user, payload.provider)
    audit(
        db,
        actor=auth.user,
        action="integration.voice.provider.set",
        target_type="integration",
        target_id=payload.provider,
        request_id=getattr(request.state, "request_id", None),
    )
    db.commit()
    return VoiceSettingsView(provider=voice_provider(db, auth.user))


@router.get("/integrations/openai", response_model=OpenAIIntegrationView)
def openai_presence(
    request: Request,
    owner: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> OpenAIIntegrationView:
    return OpenAIIntegrationView(configured=_service(request).configured(db, owner))


@router.get("/integrations/openai/voice", response_model=OpenAIVoiceSettingsView)
def openai_voice_settings(
    owner: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> OpenAIVoiceSettingsView:
    return OpenAIVoiceSettingsView(voice_id=openai_voice_id(db, owner))


@router.put("/integrations/openai/voice", response_model=OpenAIVoiceSettingsView)
def update_openai_voice_settings(
    payload: OpenAIVoiceSettingsView,
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    _: str = Depends(require_idempotency),
    db: Session = Depends(get_db),
) -> OpenAIVoiceSettingsView:
    set_openai_voice_id(db, auth.user, payload.voice_id)
    _audit(db, request, auth.user, "integration.openai.voice.set")
    db.commit()
    return OpenAIVoiceSettingsView(voice_id=openai_voice_id(db, auth.user))


def _profile_voice_view(db: Session, owner: User, profile_id: str) -> OpenAIProfileVoiceView:
    if db.get(ProfileRef, profile_id) is None:
        raise NotFoundError("The selected agent is unavailable")
    return OpenAIProfileVoiceView(
        profile_id=profile_id,
        voice_id=openai_voice_id(db, owner, profile_id),
        inherited=db.get(OpenAIProfileVoicePreference, (owner.id, profile_id)) is None,
    )


@router.get("/integrations/openai/profiles/{profile_id}/voice", response_model=OpenAIProfileVoiceView)
def profile_voice_settings(
    profile_id: Annotated[str, Path(min_length=1, max_length=36)],
    owner: User = Depends(current_user), db: Session = Depends(get_db),
) -> OpenAIProfileVoiceView:
    return _profile_voice_view(db, owner, profile_id)


@router.put("/integrations/openai/profiles/{profile_id}/voice", response_model=OpenAIProfileVoiceView)
def update_profile_voice_settings(
    profile_id: Annotated[str, Path(min_length=1, max_length=36)],
    payload: OpenAIVoiceSettingsView,
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    _: str = Depends(require_idempotency),
    db: Session = Depends(get_db),
) -> OpenAIProfileVoiceView:
    _profile_voice_view(db, auth.user, profile_id)
    set_profile_openai_voice_id(db, auth.user, profile_id, payload.voice_id)
    audit(db, actor=auth.user, action="integration.openai.profile-voice.set",
          target_type="profile", target_id=profile_id,
          request_id=getattr(request.state, "request_id", None))
    db.commit()
    return _profile_voice_view(db, auth.user, profile_id)


@router.delete("/integrations/openai/profiles/{profile_id}/voice", response_model=OpenAIProfileVoiceView)
def delete_profile_voice_settings(
    profile_id: Annotated[str, Path(min_length=1, max_length=36)],
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    _: str = Depends(require_idempotency),
    db: Session = Depends(get_db),
) -> OpenAIProfileVoiceView:
    _profile_voice_view(db, auth.user, profile_id)
    set_profile_openai_voice_id(db, auth.user, profile_id, None)
    audit(db, actor=auth.user, action="integration.openai.profile-voice.delete",
          target_type="profile", target_id=profile_id,
          request_id=getattr(request.state, "request_id", None))
    db.commit()
    return _profile_voice_view(db, auth.user, profile_id)


@router.put("/integrations/openai/key", response_model=OpenAIIntegrationView)
def set_openai_key(
    payload: OpenAIKeyMutation,
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    _: str = Depends(require_idempotency),
    db: Session = Depends(get_db),
) -> OpenAIIntegrationView:
    _service(request).set_api_key(db, auth.user, payload.api_key.get_secret_value())
    _audit(db, request, auth.user, "integration.openai.key.set")
    db.commit()
    return OpenAIIntegrationView(configured=True)


@router.delete("/integrations/openai/key", status_code=204)
def delete_openai_key(
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    _: str = Depends(require_idempotency),
    db: Session = Depends(get_db),
) -> Response:
    _service(request).delete_api_key(db, auth.user)
    _audit(db, request, auth.user, "integration.openai.key.delete")
    db.commit()
    return Response(status_code=204)


@router.post("/realtime/live-session", response_model=LiveSessionView, status_code=201)
async def create_live_session(
    payload: LiveSessionRequest,
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> LiveSessionView:
    owner = auth.user
    profile = db.get(ProfileRef, payload.profile_id)
    gateway = db.get(Gateway, profile.gateway_id) if profile else None
    if profile is None or gateway is None or not gateway.enabled:
        raise NotFoundError("The selected agent is unavailable")
    conversation = None
    if payload.session_id is not None:
        conversation = db.scalar(
            select(SessionLink).where(
                SessionLink.id == payload.session_id,
                SessionLink.owner_id == owner.id,
                SessionLink.archived_at.is_(None),
                SessionLink.gateway_id == profile.gateway_id,
                SessionLink.profile_name == profile.profile_name,
            )
        )
        if conversation is None:
            raise NotFoundError("The selected conversation is unavailable")
    settings = request.app.state.services.settings
    require_mutable_profile(
        profile.profile_name,
        "prompt.submit",
        settings.mutable_profiles,
        settings.interactive_profiles,
        managed_by_control=profile.managed_by_control,
    )
    if voice_provider(db, owner) != "openai_live":
        raise IntegrationError(
            status_code=409,
            code="LIVE_VOICE_NOT_SELECTED",
            message="Select GPT-Live-1 in voice settings before connecting",
        )
    try:
        request.app.state.live_session_limiter.consume(owner.id)
        api_key = _service(request).api_key(db, owner)
        await require_capability(
            db, request.app.state.services, gateway_id=profile.gateway_id,
            profile_name=profile.profile_name, method="prompt.submit",
        )
        history = []
        if conversation is not None:
            history = await SessionService(request.app.state.services).history(
                db, owner, conversation
            )
        result = await request.app.state.openai_live_client.create_session(
            api_key=api_key, sdp=payload.sdp, history=history,
            voice_id=openai_voice_id(db, owner, profile.id),
            agent_context=await live_agent_context(
                db, request.app.state.services, owner, profile, conversation,
            ),
        )
    except IntegrationError:
        _audit(db, request, owner, "integration.openai.live.create", failed=True)
        db.commit()
        raise
    # SDP, live session IDs, profile hints, audio, transcript and credentials
    # are excluded from both audit records and idempotency persistence.
    _audit(db, request, owner, "integration.openai.live.create")
    db.commit()
    return LiveSessionView.model_validate(result)


@router.post("/realtime/live-voice-preview", response_model=LiveSessionView, status_code=201)
async def create_live_voice_preview(
    payload: LiveVoicePreviewRequest,
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> LiveSessionView:
    owner = auth.user
    try:
        request.app.state.live_session_limiter.consume(owner.id)
        api_key = _service(request).api_key(db, owner)
        result = await request.app.state.openai_live_client.create_voice_preview(
            api_key=api_key, sdp=payload.sdp,
            voice_id=payload.voice_id, language=payload.language,
        )
    except IntegrationError:
        _audit(db, request, owner, "integration.openai.voice.preview", failed=True)
        db.commit()
        raise
    _audit(db, request, owner, "integration.openai.voice.preview")
    db.commit()
    return LiveSessionView.model_validate(result)
