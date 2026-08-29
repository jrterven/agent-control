from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from ..auth import current_user, get_db, require_csrf, require_idempotency
from ..integration_schemas import (
    ElevenLabsIntegrationTestView,
    ElevenLabsIntegrationView,
    ElevenLabsKeyMutation,
    TranscriptionTokenRequest,
    TranscriptionTokenView,
)
from ..integrations import (
    ELEVENLABS_PROVIDER,
    SCRIBE_REALTIME_MODEL_ID,
    IntegrationError,
    UserIntegrationService,
    token_expiration,
)
from ..models import AuthSession, User
from ..services import audit


router = APIRouter(tags=["integrations"])


def _service(request: Request) -> UserIntegrationService:
    return UserIntegrationService(request.app.state.services.vault)


def _view(configured: bool) -> ElevenLabsIntegrationView:
    return ElevenLabsIntegrationView(configured=configured)


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
    return _view(_service(request).is_configured(db, owner))


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
    return _view(True)


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
