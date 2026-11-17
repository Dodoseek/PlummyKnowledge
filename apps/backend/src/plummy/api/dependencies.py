"""FastAPI dependencies that expose lifespan-owned resources to routers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from plummy.application.health import CheckReadiness
from plummy.composition import Resources


def get_resources(request: Request) -> Resources:
    resources: Resources = request.app.state.resources
    return resources


ResourcesDep = Annotated[Resources, Depends(get_resources)]


def get_readiness(resources: ResourcesDep) -> CheckReadiness:
    return resources.readiness


async def get_db_session(resources: ResourcesDep) -> AsyncIterator[AsyncSession]:
    """One session per request. Use cases open transactions explicitly (``async with session.begin()``);
    anything not committed is rolled back when the session closes."""
    async with resources.session_factory() as session:
        yield session


ReadinessDep = Annotated[CheckReadiness, Depends(get_readiness)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
