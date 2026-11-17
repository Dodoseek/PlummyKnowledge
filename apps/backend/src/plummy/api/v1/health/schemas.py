from typing import Literal

from pydantic import BaseModel

from plummy.application.health import CheckStatus


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    """``{"status": "ready"}`` or ``{"status": "not_ready", "checks": {...}}`` (API_CONTRACTS.md §2.1).

    Only check names and ok/fail are exposed: no versions, hosts or error texts.
    """

    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckStatus] | None = None
