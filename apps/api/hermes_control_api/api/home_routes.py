from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from ..auth import current_user, get_db, require_csrf
from ..models import User
from ..home import home_view, save_layout
from ..home_schemas import HomeLayout

router = APIRouter(prefix="/api/v1/home", tags=["home"])


@router.get("")
def read_home(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return home_view(db, user.id, request.app.state.services)


@router.put("/layout", dependencies=[Depends(require_csrf)])
def update_home(layout: HomeLayout, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    save_layout(db, user.id, layout, request.app.state.services.settings.deployment_mode == "cloud")
    return home_view(db, user.id, request.app.state.services)


from typing import Literal
from ..schemas import ApiModel
from ..home_schemas import PanelDefinition
from ..home_panels import preview_panel


class PanelPreviewRequest(ApiModel):
    panel: PanelDefinition
    language: Literal["en", "es", "fr", "de", "pt"] = "es"


@router.post("/panels/preview", dependencies=[Depends(require_csrf)])
async def preview(payload: PanelPreviewRequest, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return await preview_panel(db, request.app.state.services, user.id, payload.panel, payload.language)


class SuggestionAction(ApiModel):
    action: Literal['seen', 'snooze', 'dismiss', 'resolve']


@router.post('/suggestions/{identifier}', dependencies=[Depends(require_csrf)])
def suggestion_action(identifier: str, payload: SuggestionAction, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from datetime import datetime, timedelta, time, timezone
    from zoneinfo import ZoneInfo
    from sqlalchemy import select
    from fastapi import HTTPException
    from ..home import layout_for
    from ..home_models import HomeSuggestion
    row = db.scalar(select(HomeSuggestion).where(HomeSuggestion.id == identifier, HomeSuggestion.owner_id == user.id))
    if row is None:
        raise HTTPException(404, 'HOME_SUGGESTION_NOT_FOUND')
    now = datetime.now(timezone.utc)
    if payload.action == 'seen':
        row.seen_at = now
    elif payload.action == 'snooze' and row.status == 'open':
        zone = ZoneInfo(layout_for(db, user.id).proactive.timezone)
        tomorrow = now.astimezone(zone).date() + timedelta(days=1)
        row.snoozed_until = datetime.combine(tomorrow, time(9), tzinfo=zone).astimezone(timezone.utc)
    elif payload.action in {'dismiss', 'resolve'}:
        row.status = 'dismissed' if payload.action == 'dismiss' else 'resolved'
        row.snoozed_until = None
    db.commit()
    return {'status': row.status}


@router.post('/review/retry', dependencies=[Depends(require_csrf)])
def retry_review(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from datetime import datetime, timezone
    from ..home_models import HomePreference
    row = db.get(HomePreference, user.id)
    if row:
        row.last_error = None
        row.next_review_at = datetime.now(timezone.utc)
        db.commit()  # Preserve last_analysis_at and lease: retry cannot bypass the cost gate.
    return {'status': 'pending'}
