"""Root API router. All public endpoints live under ``/api/v1`` (API_CONTRACTS.md §1.1)."""

from fastapi import APIRouter

from plummy.api.v1.router import router as v1_router

API_PREFIX = "/api"

api_router = APIRouter(prefix=API_PREFIX)
api_router.include_router(v1_router)
