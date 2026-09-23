from fastapi import APIRouter, Depends, Request

from ..auth import current_user, require_csrf
from ..models import AuthSession, User
from ..schemas import ApiModel

router = APIRouter(prefix="/api/v1/search/semantic", tags=["search"])


class SemanticSettings(ApiModel):
    enabled: bool


@router.get("/status")
def status(request: Request, user: User = Depends(current_user)):
    return request.app.state.semantic_search.status(user.id)


@router.put("/settings")
def settings(payload: SemanticSettings, request: Request, auth: AuthSession = Depends(require_csrf)):
    return request.app.state.semantic_search.settings(auth.user.id, payload.enabled)
