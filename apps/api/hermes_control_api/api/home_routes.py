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
