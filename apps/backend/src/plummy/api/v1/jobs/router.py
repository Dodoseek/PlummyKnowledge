"""``/api/v1/jobs`` (API_CONTRACTS.md §2.15). ``{type}`` is ``ingestion`` or ``sync``; without ``type``
the list holds ingestion jobs (each type is its own keyset). Sync jobs are not retried here: a sync runs
again on its schedule or by ``POST /data-sources/{id}/sync``. Cancel is not implemented yet."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import client_info, require_permission
from plummy.api.v1.common import allow_query, no_store, page_request
from plummy.api.v1.data_sources.schemas import SyncJobResponse
from plummy.api.v1.jobs.schemas import JobListResponse, JobResponse, SyncJobListResponse
from plummy.application.jobs.use_cases import JobNotRetryableError
from plummy.application.pagination import SortSpec
from plummy.application.security import SecurityContext
from plummy.domain.documents import JobStatus
from plummy.domain.identity import Permission

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(no_store)])

SORTS = (
    SortSpec("-queued_at", "queued_at", True, datetime),
    SortSpec("queued_at", "queued_at", False, datetime),
)
JobType = Literal["ingestion", "sync"]

Reader = Annotated[SecurityContext, Depends(require_permission(Permission.JOBS_READ))]
Manager = Annotated[SecurityContext, Depends(require_permission(Permission.JOBS_MANAGE))]


@router.get(
    "",
    response_model=JobListResponse | SyncJobListResponse,
    dependencies=[
        Depends(
            allow_query("type", "status", "knowledge_base_id", "data_source_id", "sort", "limit", "cursor")
        )
    ],
)
async def list_jobs(
    context: Reader,
    resources: ResourcesDep,
    job_type: Annotated[JobType | None, Query(alias="type")] = None,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    knowledge_base_id: UUID | None = None,
    data_source_id: UUID | None = None,
    sort: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> JobListResponse | SyncJobListResponse:
    page = page_request(sort, limit, cursor, SORTS, SORTS[0])
    if job_type == "sync":
        synced = await resources.job_admin.list_sync.execute(
            context,
            page,
            status=status_filter,
            knowledge_base_id=knowledge_base_id,
            data_source_id=data_source_id,
        )
        return SyncJobListResponse.from_page(synced)
    result = await resources.job_admin.list.execute(
        context,
        page,
        status=status_filter,
        knowledge_base_id=knowledge_base_id,
        data_source_id=data_source_id,
    )
    return JobListResponse.from_page(result)


@router.get("/{job_type}/{job_id}", response_model=JobResponse | SyncJobResponse)
async def get_job(
    job_type: JobType, job_id: UUID, context: Reader, resources: ResourcesDep
) -> JobResponse | SyncJobResponse:
    if job_type == "sync":
        return SyncJobResponse.build(await resources.job_admin.get_sync.execute(context, job_id))
    return JobResponse.build(await resources.job_admin.get.execute(context, job_id))


@router.post("/{job_type}/{job_id}/retry", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def retry_job(
    job_type: JobType, job_id: UUID, request: Request, context: Manager, resources: ResourcesDep
) -> JobResponse:
    if job_type == "sync":
        await resources.job_admin.get_sync.execute(context, job_id)  # 404 for an unknown job
        raise JobNotRetryableError("Sync jobs are not retried; start a sync of the source instead.")
    job = await resources.job_admin.retry.execute(context, client_info(request), job_id)
    return JobResponse.build(job)
