"""Index phase of the pipeline (ADR-008 §3, stages 8-12), on the ``embeddings`` queue.

- chunk (8): the stored ``NormalizedDocument`` is chunked into a new, inactive ``chunk_set``;
- embed (9): vectors for every embedding profile of the knowledge bases that include the source,
  through the configured provider (``SafeHttpClient``, ADR-007), written batch by batch so a retry
  resumes;
- persist (10): every chunk has a vector for every profile;
- finalize (11): one transaction makes the version ``ready`` with the new set active and the document's
  current version, the previous version ``outdated`` and the job ``succeeded``: retrieval switches
  atomically.

Retrieval never sees a set before step 11. Rows are inserted with ``ON CONFLICT DO NOTHING``, so a retried or
concurrent run of the job never duplicates chunks or vectors (SC-09).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from plummy.application.ai.ports import (
    CancellationToken,
    EmbeddingProvider,
    ProviderError,
    ProviderGateway,
    UnsupportedCapability,
)
from plummy.application.errors import ServiceUnavailableError
from plummy.application.ingestion.chunking import ChunkingError, ChunkingSettings, ChunkingStrategy
from plummy.application.ingestion.parsing import NormalizedDocument
from plummy.application.ingestion.pipeline import (
    JobRunner,
    PermanentIngestionError,
    Phase,
    RunOutcome,
    TransientIngestionError,
    Work,
)
from plummy.application.ports import (
    Clock,
    JobQueue,
    JobQueueUnavailableError,
    NewEmbedding,
    UnitOfWorkFactory,
    VectorIndexManager,
)
from plummy.application.providers.use_cases import connection_for
from plummy.application.secret_storage.vault import SecretVault
from plummy.application.storage import StorageArea, StorageKey, StorageObjectNotFoundError, StorageProvider
from plummy.domain.ai import EmbeddingProfile, EmbeddingProfileStatus
from plummy.domain.documents import DocumentVersion, JobResult, JobStage, JobStatus
from plummy.domain.knowledge import KnowledgeBase

logger = logging.getLogger(__name__)

MAX_EMBEDDING_BATCH: Final = 64
MAX_EXTRACTED_BYTES: Final = 256 * 1024 * 1024


class RunIndexingJob(JobRunner):
    phase = Phase.INDEX

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        storage: StorageProvider,
        clock: Clock,
        *,
        chunker: ChunkingStrategy,
        gateway: ProviderGateway,
        vault: SecretVault,
        indexes: VectorIndexManager,
        jobs: JobQueue,
    ) -> None:
        super().__init__(uow, storage, clock)
        self._chunker, self._gateway, self._vault = chunker, gateway, vault
        self._indexes, self._jobs = indexes, jobs

    def _ready_for_phase(self, version: DocumentVersion) -> bool:
        # Only after the extract phase stored the normalized document (a stray message is ignored).
        return version.extracted_storage_key is not None

    async def _run(self, work: Work) -> RunOutcome:
        knowledge_bases = await self._linked_knowledge_bases(work)
        profiles = await self._profiles(knowledge_bases)
        settings = ChunkingSettings().limited_to(
            min((p.max_input_tokens for p in profiles if p.max_input_tokens), default=None)
        )
        chunk_set = await self._chunk(work, settings)
        await self._set_stage(work, JobStage.EMBED)
        for profile in profiles:
            await self._embed(work, chunk_set, profile)
        await self._set_stage(work, JobStage.PERSIST)
        await self._verify(work, chunk_set, profiles)
        await self._set_stage(work, JobStage.FINALIZE)
        return await self._activate(work, chunk_set)

    async def _profiles(self, knowledge_bases: Sequence[KnowledgeBase]) -> list[EmbeddingProfile]:
        """The distinct profiles of the knowledge bases, in a stable order."""
        profiles: list[EmbeddingProfile] = []
        async with self._uow() as uow:
            for profile_id in sorted({kb.embedding_profile_id for kb in knowledge_bases}):
                profile = await uow.embedding_profiles.get(profile_id)
                if profile is None or profile.status is EmbeddingProfileStatus.RETIRED:
                    raise PermanentIngestionError(
                        "embedding_profile_unavailable",
                        "The embedding profile of the knowledge base is retired.",
                    )
                profiles.append(profile)
        return profiles

    async def _chunk(self, work: Work, settings: ChunkingSettings) -> int:
        """Stage 8: the unfinished set of an earlier attempt, or a new one after the active set."""
        await self._set_stage(work, JobStage.CHUNK)
        version = work.version
        async with self._uow() as uow:
            sets = await uow.chunks.chunk_sets(version.id)
        last = sets[-1] if sets else 0
        if last and last != version.active_chunk_set:
            return last  # chunks are inserted in one transaction: an existing set is complete
        chunk_set = last + 1
        try:
            drafts = self._chunker.chunk(await self._load_document(version), settings)
        except ChunkingError as exc:
            raise PermanentIngestionError(exc.code, exc.message) from None
        if not drafts:
            raise PermanentIngestionError("no_text", "The document contains no extractable text.")
        async with self._uow() as uow:
            await self._guard(uow, work)
            await uow.chunks.add_chunks(
                document_id=work.document.id,
                version_id=version.id,
                data_source_id=work.document.data_source_id,
                chunk_set=chunk_set,
                drafts=drafts,
            )
            await uow.commit()
        return chunk_set

    async def _load_document(self, version: DocumentVersion) -> NormalizedDocument:
        key = StorageKey(version.extracted_storage_key or "")
        try:
            async with self._storage.local_path(StorageArea.DOCUMENTS, key) as path:
                data = await asyncio.to_thread(_read_json, path)
            return NormalizedDocument.from_json(data)
        except StorageObjectNotFoundError:
            raise PermanentIngestionError(
                "extraction_missing", "The extracted text is no longer available."
            ) from None
        except (ValueError, KeyError, TypeError):
            raise PermanentIngestionError(
                "extraction_invalid", "The extracted text could not be read."
            ) from None

    async def _embed(self, work: Work, chunk_set: int, profile: EmbeddingProfile) -> None:
        """Stage 9 for one profile: the chunks without a vector, batch by batch (a retry resumes)."""
        await self._request_index(profile)
        provider = await self._provider(profile)
        batch_size = max(1, min(MAX_EMBEDDING_BATCH, provider.embedding_capabilities().max_batch))
        while True:
            async with self._uow() as uow:
                batch = await uow.chunks.without_embedding(
                    work.version.id, chunk_set, profile.id, limit=batch_size
                )
            if not batch:
                return
            try:
                vectors = await provider.embed(
                    [chunk.content for chunk in batch], model_id=profile.model_id, cancel=CancellationToken()
                )
            except ProviderError as exc:
                if exc.retryable:
                    raise TransientIngestionError(exc.code.value) from None
                raise PermanentIngestionError(exc.code.value, exc.message) from None
            if len(vectors) != len(batch):
                raise TransientIngestionError("provider returned a different number of vectors")
            rows = [
                NewEmbedding(chunk.id, _checked(vector, profile))
                for chunk, vector in zip(batch, vectors, strict=True)
            ]
            async with self._uow() as uow:
                await self._guard(uow, work)
                await uow.chunks.add_embeddings(
                    profile_id=profile.id,
                    version_id=work.version.id,
                    data_source_id=work.document.data_source_id,
                    dimensions=profile.dimensions,
                    embeddings=rows,
                )
                await uow.commit()

    async def _request_index(self, profile: EmbeddingProfile) -> None:
        """The HNSW index should exist before vectors arrive (ADR-017 §2); it is built off this job."""
        if await self._indexes.is_ready(profile):
            return
        try:
            await self._jobs.enqueue_vector_index(profile.id)
        except JobQueueUnavailableError:
            logger.warning("vector index request not sent", extra={"embedding_profile_id": str(profile.id)})

    async def _provider(self, profile: EmbeddingProfile) -> EmbeddingProvider:
        async with self._uow() as uow:
            provider = await uow.providers.get(profile.provider_id)
            if provider is None or provider.deleted_at is not None or not provider.enabled:
                raise PermanentIngestionError(
                    "provider_unavailable", "The embedding provider is disabled or deleted."
                )
            try:
                connection = await connection_for(uow, self._vault, provider)
            except ServiceUnavailableError:
                raise PermanentIngestionError(
                    "secret_unavailable", "The provider key cannot be decrypted."
                ) from None
        try:
            return self._gateway.embedding(connection)
        except UnsupportedCapability:
            raise PermanentIngestionError(
                "capability_not_supported", "The provider has no embeddings API."
            ) from None

    async def _verify(self, work: Work, chunk_set: int, profiles: Sequence[EmbeddingProfile]) -> None:
        """Stage 10: every chunk of the set has a vector for every profile."""
        async with self._uow() as uow:
            chunks = await uow.chunks.count(work.version.id, chunk_set)
            for profile in profiles:
                if await uow.chunks.embedding_count(work.version.id, chunk_set, profile.id) != chunks:
                    raise TransientIngestionError("vectors are missing")

    async def _activate(self, work: Work, chunk_set: int) -> RunOutcome:
        """Stages 11-12 in one transaction: the new set becomes visible together with the finished job."""
        now = self._clock.now()
        async with self._uow() as uow:
            document = await uow.documents.get_for_update(work.document.id)
            job = await uow.ingestion_jobs.get_for_update(work.job.id)
            if job is None or job.status.finished:
                return RunOutcome.SKIPPED  # a concurrent run finished it
            if document is None or document.deleted_at is not None:
                job.status, job.error_code, job.finished_at = JobStatus.CANCELLED, "resource_unavailable", now
                await uow.ingestion_jobs.save(job)
                await uow.commit()
                return RunOutcome.CANCELLED
            await uow.documents.activate_version(
                work.document.id, work.version.id, chunk_set=chunk_set, at=now
            )
            job.status, job.result = JobStatus.SUCCEEDED, JobResult.PROCESSED
            job.error_code = job.error_message = None
            job.finished_at = now
            await uow.ingestion_jobs.save(job)
            await uow.commit()
        return RunOutcome.SUCCEEDED


def _read_json(path: Path) -> Any:
    if path.stat().st_size > MAX_EXTRACTED_BYTES:
        raise PermanentIngestionError("extraction_invalid", "The extracted text is too large.")
    return json.loads(path.read_bytes())


def _checked(vector: Sequence[float], profile: EmbeddingProfile) -> tuple[float, ...]:
    """The vector size must match the profile (ADR-017 §1); ``normalize`` makes it unit length (L2)."""
    if len(vector) != profile.dimensions:
        raise PermanentIngestionError(
            "embedding_dimensions_mismatch",
            f"The provider returned {len(vector)} dimensions, the profile expects {profile.dimensions}.",
        )
    values = tuple(float(value) for value in vector)
    if not all(math.isfinite(value) for value in values):
        raise PermanentIngestionError(
            "embedding_invalid_response", "The provider returned an invalid vector."
        )
    if profile.normalize:
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            raise PermanentIngestionError(
                "embedding_invalid_response", "The provider returned a zero vector."
            )
        values = tuple(value / norm for value in values)
    return values
