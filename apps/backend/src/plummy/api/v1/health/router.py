"""Health endpoints (public scope). Used by Compose healthchecks and the frontend status screen."""

from fastapi import APIRouter, Response, status

from plummy.api.dependencies import ReadinessDep
from plummy.api.v1.health.schemas import LivenessResponse, ReadinessResponse

router = APIRouter(prefix="/health", tags=["health"])

_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/live", summary="Process is alive")
async def live(response: Response) -> LivenessResponse:
    """Does not touch dependencies: a failing database must not get the process restarted."""
    response.headers.update(_NO_STORE)
    return LivenessResponse()


@router.get(
    "/ready",
    summary="Required dependencies are available",
    response_model_exclude_none=True,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def ready(response: Response, readiness: ReadinessDep) -> ReadinessResponse:
    response.headers.update(_NO_STORE)
    report = await readiness.execute()
    if report.ready:
        return ReadinessResponse(status="ready")
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="not_ready", checks=report.checks)
