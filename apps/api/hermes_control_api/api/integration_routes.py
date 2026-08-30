from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..auth import current_user, get_db, require_csrf, require_idempotency
from ..integration_schemas import (
    ElevenLabsIntegrationTestView,
    ElevenLabsIntegrationView,
    ElevenLabsKeyMutation,
    ElevenLabsVoiceListView,
    ElevenLabsVoiceMutation,
    ElevenLabsVoiceView,
    SpeechRequest,
    SpeechTokenRequest,
    SpeechTokenView,
    TranscriptionTokenRequest,
    TranscriptionTokenView,
)
from ..integrations import (
    ELEVENLABS_PROVIDER,
    ELEVENLABS_MAX_PREVIEW_RESPONSE_BYTES,
    SCRIBE_REALTIME_MODEL_ID,
    IntegrationError,
    SpeechVoicePreviewUnavailable,
    SpeechVoiceUnavailable,
    UserIntegrationService,
    token_expiration,
)
from ..models import AuthSession, User
from ..services import audit


router = APIRouter(tags=["integrations"])


def _service(request: Request) -> UserIntegrationService:
    return UserIntegrationService(request.app.state.services.vault)


def _view(configuration: dict[str, object]) -> ElevenLabsIntegrationView:
    return ElevenLabsIntegrationView(**configuration)


def _audit_integration(
    db: Session,
    *,
    actor: User,
    request: Request,
    action: str,
    outcome: str = "success",
) -> None:
    # Provider identity is the only integration detail written to the audit
    # trail. Credentials, tokens, transcripts and session hints are excluded.
    audit(
        db,
        actor=actor,
        action=action,
        target_type="integration",
        target_id=ELEVENLABS_PROVIDER,
        outcome=outcome,
        request_id=getattr(request.state, "request_id", None),
    )


@router.get(
    "/api/v1/integrations/elevenlabs",
    response_model=ElevenLabsIntegrationView,
)
def elevenlabs_presence(
    request: Request,
    owner: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ElevenLabsIntegrationView:
    return _view(_service(request).configuration(db, owner))


@router.put(
    "/api/v1/integrations/elevenlabs/key",
    response_model=ElevenLabsIntegrationView,
)
def set_elevenlabs_key(
    payload: ElevenLabsKeyMutation,
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    db: Session = Depends(get_db),
) -> ElevenLabsIntegrationView:
    owner = auth.user
    _service(request).set_api_key(db, owner, payload.api_key.get_secret_value())
    _audit_integration(
        db,
        actor=owner,
        request=request,
        action="integration.elevenlabs.key.set",
    )
    db.commit()
    return _view(_service(request).configuration(db, owner))


@router.post(
    "/api/v1/integrations/elevenlabs/test",
    response_model=ElevenLabsIntegrationTestView,
)
async def test_elevenlabs_key(
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    db: Session = Depends(get_db),
) -> ElevenLabsIntegrationTestView:
    owner = auth.user
    try:
        api_key = _service(request).api_key(db, owner)
        request.app.state.transcription_token_limiter.consume(owner.id)
        # Successful issuance validates the exact capability Agent Control
        # needs. The single-use token is deliberately discarded.
        await request.app.state.elevenlabs_scribe_client.issue_realtime_token(api_key)
    except IntegrationError:
        _audit_integration(
            db,
            actor=owner,
            request=request,
            action="integration.elevenlabs.test",
            outcome="failure",
        )
        db.commit()
        raise
    _audit_integration(
        db,
        actor=owner,
        request=request,
        action="integration.elevenlabs.test",
    )
    db.commit()
    return ElevenLabsIntegrationTestView()


@router.delete("/api/v1/integrations/elevenlabs/key", status_code=204)
def delete_elevenlabs_key(
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    db: Session = Depends(get_db),
) -> Response:
    owner = auth.user
    _service(request).delete_api_key(db, owner)
    _audit_integration(
        db,
        actor=owner,
        request=request,
        action="integration.elevenlabs.key.delete",
    )
    db.commit()
    return Response(status_code=204)


@router.post(
    "/api/v1/realtime/transcription-token",
    response_model=TranscriptionTokenView,
)
async def issue_transcription_token(
    payload: TranscriptionTokenRequest,
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TranscriptionTokenView:
    # ``payload`` validates the public contract. Neither optional field is
    # forwarded to token issuance or persisted in the audit trail.
    del payload
    owner = auth.user
    try:
        api_key = _service(request).api_key(db, owner)
        request.app.state.transcription_token_limiter.consume(owner.id)
        token = await request.app.state.elevenlabs_scribe_client.issue_realtime_token(
            api_key
        )
    except IntegrationError:
        _audit_integration(
            db,
            actor=owner,
            request=request,
            action="integration.elevenlabs.token.issue",
            outcome="failure",
        )
        db.commit()
        raise
    _audit_integration(
        db,
        actor=owner,
        request=request,
        action="integration.elevenlabs.token.issue",
    )
    db.commit()
    return TranscriptionTokenView(
        token=token,
        expires_at=token_expiration(),
        model_id=SCRIBE_REALTIME_MODEL_ID,
    )


@router.get(
    "/api/v1/integrations/elevenlabs/voices",
    response_model=ElevenLabsVoiceListView,
)
async def list_elevenlabs_voices(
    request: Request,
    owner: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ElevenLabsVoiceListView:
    api_key = _service(request).api_key(db, owner)
    request.app.state.speech_rate_limiter.consume(owner.id)
    voices = await request.app.state.elevenlabs_speech_client.list_voices(api_key)
    return ElevenLabsVoiceListView(
        items=[
            ElevenLabsVoiceView(
                id=str(voice["id"]),
                name=str(voice["name"]),
                category=str(voice["category"]) if voice.get("category") else None,
                labels=dict(voice.get("labels") or {}),
                preview_available=voice.get("preview_available") is True,
            )
            for voice in voices
        ]
    )


@router.get("/api/v1/integrations/elevenlabs/voice-preview/{voice_id}")
async def preview_elevenlabs_voice(
    voice_id: Annotated[
        str,
        Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
    ],
    request: Request,
    owner: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    service = _service(request)
    api_key = service.api_key(db, owner)
    request.app.state.speech_rate_limiter.consume(owner.id)
    voices = await request.app.state.elevenlabs_speech_client.list_voices(api_key)
    selected = next((voice for voice in voices if voice["id"] == voice_id), None)
    preview_url = selected.get("preview_url") if selected else None
    if not isinstance(preview_url, str):
        raise SpeechVoicePreviewUnavailable()
    upstream, own_client = (
        await request.app.state.elevenlabs_speech_client.open_preview_stream(
            preview_url
        )
    )
    return StreamingResponse(
        request.app.state.elevenlabs_speech_client.audio_chunks(
            upstream,
            own_client,
            ELEVENLABS_MAX_PREVIEW_RESPONSE_BYTES,
        ),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": 'inline; filename="voice-preview.mp3"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.put(
    "/api/v1/integrations/elevenlabs/voice",
    response_model=ElevenLabsIntegrationView,
)
async def set_elevenlabs_voice(
    payload: ElevenLabsVoiceMutation,
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    db: Session = Depends(get_db),
) -> ElevenLabsIntegrationView:
    owner = auth.user
    try:
        api_key = _service(request).api_key(db, owner)
        request.app.state.speech_rate_limiter.consume(owner.id)
        voices = await request.app.state.elevenlabs_speech_client.list_voices(api_key)
        selected = next(
            (voice for voice in voices if voice["id"] == payload.voice_id),
            None,
        )
        if selected is None:
            raise SpeechVoiceUnavailable()
        _service(request).set_voice(
            db,
            owner,
            voice_id=str(selected["id"]),
            voice_name=str(selected["name"]),
            model_id=payload.tts_model_id,
        )
    except IntegrationError:
        _audit_integration(
            db,
            actor=owner,
            request=request,
            action="integration.elevenlabs.voice.set",
            outcome="failure",
        )
        db.commit()
        raise
    _audit_integration(
        db,
        actor=owner,
        request=request,
        action="integration.elevenlabs.voice.set",
    )
    db.commit()
    return _view(_service(request).configuration(db, owner))


@router.post(
    "/api/v1/realtime/speech-token",
    response_model=SpeechTokenView,
)
async def issue_speech_token(
    payload: SpeechTokenRequest,
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> SpeechTokenView:
    del payload
    owner = auth.user
    try:
        service = _service(request)
        api_key = service.api_key(db, owner)
        voice_id, voice_name, model_id = service.speech_configuration(db, owner)
        request.app.state.speech_rate_limiter.consume(owner.id)
        token = await request.app.state.elevenlabs_speech_client.issue_realtime_token(
            api_key
        )
    except IntegrationError:
        _audit_integration(
            db,
            actor=owner,
            request=request,
            action="integration.elevenlabs.speech-token.issue",
            outcome="failure",
        )
        db.commit()
        raise
    _audit_integration(
        db,
        actor=owner,
        request=request,
        action="integration.elevenlabs.speech-token.issue",
    )
    db.commit()
    return SpeechTokenView(
        token=token,
        expires_at=token_expiration(),
        model_id=model_id,
        voice_id=voice_id,
        voice_name=voice_name,
    )


@router.post("/api/v1/integrations/elevenlabs/speech")
async def stream_elevenlabs_speech(
    payload: SpeechRequest,
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    owner = auth.user
    try:
        service = _service(request)
        api_key = service.api_key(db, owner)
        voice_id, _, model_id = service.speech_configuration(db, owner)
        request.app.state.speech_rate_limiter.consume(owner.id)
        upstream, own_client = (
            await request.app.state.elevenlabs_speech_client.open_audio_stream(
                api_key,
                voice_id=voice_id,
                model_id=model_id,
                text=payload.text,
            )
        )
    except IntegrationError:
        _audit_integration(
            db,
            actor=owner,
            request=request,
            action="integration.elevenlabs.speech.generate",
            outcome="failure",
        )
        db.commit()
        raise
    _audit_integration(
        db,
        actor=owner,
        request=request,
        action="integration.elevenlabs.speech.generate",
    )
    db.commit()
    return StreamingResponse(
        request.app.state.elevenlabs_speech_client.audio_chunks(
            upstream,
            own_client,
        ),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="agent-response.mp3"',
            "X-Content-Type-Options": "nosniff",
        },
    )
