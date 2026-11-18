from fastapi import APIRouter, Depends

from plummy.api.security import verify_origin
from plummy.api.v1.auth.router import router as auth_router
from plummy.api.v1.groups.router import router as groups_router
from plummy.api.v1.health.router import router as health_router
from plummy.api.v1.roles.router import router as roles_router
from plummy.api.v1.setup.router import router as setup_router
from plummy.api.v1.system.router import router as system_router
from plummy.api.v1.users.router import router as users_router

# Every state-changing request under /api/v1 must come from a trusted origin (ADR-013 §2).
router = APIRouter(prefix="/v1", dependencies=[Depends(verify_origin)])
router.include_router(health_router)
router.include_router(setup_router)
router.include_router(auth_router)
router.include_router(users_router)
router.include_router(groups_router)
router.include_router(roles_router)
router.include_router(system_router)
