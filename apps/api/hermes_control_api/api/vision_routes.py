from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..auth import current_user, get_db, require_csrf
from ..models import AuthSession, User
from ..vision import VisionService
from ..vision_schemas import VisionAnalysisRequest, VisionAnalysisResult, VisionIntentRequest, VisionIntentResult, VisionObservationPage, VisionPreferences, VisionPreferencesMutation

router = APIRouter(prefix="/api/v1", tags=["camera vision"])


def _service(request: Request) -> VisionService:
    return request.app.state.vision_service


@router.get("/vision/preferences", response_model=VisionPreferences)
def preferences(request: Request, owner: User = Depends(current_user), db: Session = Depends(get_db)):
    return _service(request).preferences(db, owner)


@router.put("/vision/preferences", response_model=VisionPreferences)
def set_preferences(payload: VisionPreferencesMutation, request: Request, auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    return _service(request).set_preferences(db, auth.user, payload)


@router.post("/sessions/{session_id}/vision/intent", response_model=VisionIntentResult)
async def intent(session_id: str, payload: VisionIntentRequest, request: Request, auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    return await _service(request).intent(db, auth.user, session_id, payload)


@router.post("/sessions/{session_id}/vision/analyses", response_model=VisionAnalysisResult)
async def analyses(session_id: str, payload: VisionAnalysisRequest, request: Request, auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    return await _service(request).analyze(db, auth.user, session_id, payload)


@router.get("/sessions/{session_id}/vision/observations", response_model=VisionObservationPage)
def observations(session_id: str, request: Request, before: UUID | None = None, owner: User = Depends(current_user), db: Session = Depends(get_db)):
    return _service(request).observations(db, owner, session_id, str(before) if before else None)
