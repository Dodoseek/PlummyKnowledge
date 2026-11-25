from fastapi import APIRouter, Depends

from plummy.api.security import verify_origin
from plummy.api.v1.assistants.router import router as assistants_router
from plummy.api.v1.auth.router import router as auth_router
from plummy.api.v1.chat.router import router as chat_router
from plummy.api.v1.conversations.router import router as conversations_router
from plummy.api.v1.data_sources.router import router as data_sources_router
from plummy.api.v1.documents.router import router as documents_router
from plummy.api.v1.embedding_profiles.router import router as embedding_profiles_router
from plummy.api.v1.groups.router import router as groups_router
from plummy.api.v1.health.router import router as health_router
from plummy.api.v1.jobs.router import router as jobs_router
from plummy.api.v1.knowledge_bases.router import router as knowledge_bases_router
from plummy.api.v1.providers.router import router as providers_router
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
router.include_router(providers_router)
router.include_router(embedding_profiles_router)
router.include_router(knowledge_bases_router)
router.include_router(documents_router)
router.include_router(data_sources_router)
router.include_router(assistants_router)
router.include_router(conversations_router)
router.include_router(chat_router)
router.include_router(jobs_router)
router.include_router(system_router)
