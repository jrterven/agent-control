from fastapi import APIRouter

from .admin_routes import router as admin_router
from .routes import router as core_router


router = APIRouter()
router.include_router(core_router)
router.include_router(admin_router)

__all__ = ["router"]
