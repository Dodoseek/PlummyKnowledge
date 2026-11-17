from fastapi import APIRouter

from plummy.api.v1.health.router import router as health_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
