"""Ports (interfaces) implemented by infrastructure adapters (ARCHITECTURE.md §5)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from plummy.application.ingestion.chunking import ChunkDraft
from plummy.application.pagination import Page, PageRequest
from plummy.application.retrieval.model import ChunkHit, RetrievalScope
from plummy.domain.ai import AIProvider, EmbeddingProfile, ModelConfig, TestStatus
from plummy.domain.audit import AuditEntry
from plummy.domain.documents import (
    DeletionReason,
    Document,
    DocumentStatus,
    DocumentVersion,
    IngestionJob,
    JobStatus,
)
from plummy.domain.identity import Group, InstanceState, SessionRevokeReason, User, UserSession, UserStatus
from plummy.domain.knowledge import DataSource, KbAccessLevel, KnowledgeBase
from plummy.domain.secrets import SecretKind, SecretValue


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...


class PasswordHasher(Protocol):
    async def hash(self, password: str) -> str: ...

    async def verify(self, password_hash: str | None, password: str) -> bool:
        """``False`` for a mismatch. With ``None`` it still does the same amount of work (timing safety)."""
        ...

    def needs_rehash(self, password_hash: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class RateLimitStatus:
    allowed: bool
    retry_after_seconds: int


class UniqueViolationError(Exception):
    """A concurrent transaction inserted the same unique value first (email, group name)."""


class RateLimiterUnavailableError(Exception):
    """The counter store is unreachable; callers fail closed."""


class RateLimiter(Protocol):
    """Fixed-window counters."""

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitStatus:
        """Count one event and report whether it is within the limit."""
        ...

    async def peek(self, key: str, *, limit: int, window_seconds: int) -> RateLimitStatus:
        """Report whether the limit is already exhausted, without counting."""
        ...

    async def reset(self, key: str) -> None: ...


class BootstrapTokenStore(Protocol):
    """Hands the plain bootstrap token to the server operator (a file readable only on the server)."""

    @property
    def location(self) -> str: ...

    def read(self) -> str | None:
        """The token currently in the store, or ``None``."""
        ...

    def write(self, token: str) -> None: ...

    def remove(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class SessionWithUser:
    session: UserSession
    user: User


@dataclass(frozen=True, slots=True)
class UserQuery:
    """Allowlisted filters of ``GET /users`` (ADR-014). Soft-deleted users are never listed."""

    status: UserStatus | None = None
    group_id: UUID | None = None
    search: str | None = None


@dataclass(frozen=True, slots=True)
class RoleInfo:
    key: str
    name: str
    description: str
    is_system: bool
    permissions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PermissionInfo:
    key: str
    description: str


@dataclass(frozen=True, slots=True)
class SetChange[K]:
    """Result of replacing a set (roles, members): what was added and what was removed."""

    added: frozenset[K]
    removed: frozenset[K]


@dataclass(frozen=True, slots=True)
class ActivationRecord:
    user_id: UUID
    expires_at: datetime


class UserRepository(Protocol):
    async def get(self, user_id: UUID) -> User | None:
        """By id, including soft-deleted users (callers decide what a deleted user means)."""
        ...

    async def get_for_update(self, user_id: UUID) -> User | None:
        """Lock the user row until the end of the transaction."""
        ...

    async def get_by_email(self, email: str) -> User | None:
        """Lookup by canonical email among users that are not soft-deleted (disabled ones included)."""
        ...

    async def page(self, query: UserQuery, page: PageRequest) -> Page[User]: ...

    async def existing_ids(self, user_ids: frozenset[UUID]) -> frozenset[UUID]:
        """The subset of ids that belong to users that are not soft-deleted."""
        ...

    async def update(self, user: User, *, at: datetime) -> None:
        """Persist ``display_name``, ``status`` and ``deleted_at``."""
        ...

    async def add(self, user: User) -> None: ...

    async def record_login(self, user_id: UUID, *, at: datetime, new_password_hash: str | None) -> None: ...

    async def set_password(self, user_id: UUID, *, password_hash: str, changed_at: datetime) -> None: ...


class RoleRepository(Protocol):
    async def assign(self, user_id: UUID, role_key: str, *, granted_by: UUID | None) -> None: ...

    async def roles_and_permissions(self, user_id: UUID) -> tuple[frozenset[str], frozenset[str]]:
        """Effective role keys (direct and through groups that are not deleted) and their permissions."""
        ...

    async def catalog(self) -> list[RoleInfo]: ...

    async def permission_catalog(self) -> list[PermissionInfo]: ...

    async def existing_keys(self) -> frozenset[str]: ...

    async def user_role_keys(self, user_id: UUID) -> frozenset[str]:
        """Roles assigned to the user directly."""
        ...

    async def role_keys_for_users(self, user_ids: list[UUID]) -> dict[UUID, frozenset[str]]: ...

    async def set_user_roles(
        self, user_id: UUID, role_keys: frozenset[str], *, granted_by: UUID | None
    ) -> SetChange[str]: ...

    async def group_role_keys(self, group_id: UUID) -> frozenset[str]: ...

    async def set_group_roles(
        self, group_id: UUID, role_keys: frozenset[str], *, granted_by: UUID | None
    ) -> SetChange[str]: ...

    async def lock_administrators(self) -> None:
        """Serialize every change that can remove an administrator (row lock on the ``admin`` role)."""
        ...

    async def count_active_administrators(self, *, excluding: UUID | None = None) -> int:
        """Active, not deleted users with the ``admin`` role assigned directly."""
        ...


class GroupRepository(Protocol):
    async def add(self, group: Group) -> None: ...

    async def get(self, group_id: UUID) -> Group | None:
        """A group that is not soft-deleted."""
        ...

    async def get_by_name(self, name: str) -> Group | None: ...

    async def page(self, search: str | None, page: PageRequest) -> Page[Group]: ...

    async def update(self, group: Group, *, at: datetime) -> None:
        """Persist ``name``, ``description`` and ``deleted_at``."""
        ...

    async def members(self, group_id: UUID) -> list[User]:
        """Members that are not soft-deleted, ordered by email."""
        ...

    async def member_counts(self, group_ids: list[UUID]) -> dict[UUID, int]: ...

    async def set_members(self, group_id: UUID, user_ids: frozenset[UUID]) -> SetChange[UUID]: ...

    async def groups_of_user(self, user_id: UUID) -> list[Group]: ...

    async def remove_user_from_all(self, user_id: UUID) -> None: ...

    async def clear(self, group_id: UUID) -> None:
        """Remove all members and roles of the group."""
        ...


class ActivationTokenRepository(Protocol):
    async def put(
        self, user_id: UUID, token_hash: bytes, *, expires_at: datetime, created_by: UUID | None
    ) -> None:
        """Store the user's only activation token, replacing an earlier one."""
        ...

    async def find(self, token_hash: bytes) -> ActivationRecord | None: ...

    async def delete(self, user_id: UUID) -> None: ...


class SessionRepository(Protocol):
    async def add(self, session: UserSession, token_hash: bytes) -> None: ...

    async def get_by_token_hash(self, token_hash: bytes) -> SessionWithUser | None: ...

    async def get(self, session_id: UUID) -> UserSession | None: ...

    async def list_for_user(self, user_id: UUID) -> list[UserSession]:
        """Sessions of the user that are not revoked, most recently used first."""
        ...

    async def touch(self, session_id: UUID, *, at: datetime) -> None: ...

    async def revoke(self, session_id: UUID, *, reason: SessionRevokeReason, at: datetime) -> bool:
        """Revoke if still active; ``True`` if this call revoked it."""
        ...

    async def revoke_by_token_hash(
        self, token_hash: bytes, *, reason: SessionRevokeReason, at: datetime
    ) -> UUID | None: ...

    async def revoke_all_for_user(
        self, user_id: UUID, *, reason: SessionRevokeReason, at: datetime
    ) -> int: ...


class InstanceStateRepository(Protocol):
    async def get(self) -> InstanceState: ...

    async def get_for_update(self) -> InstanceState:
        """Lock the singleton row until the end of the transaction (serializes setup)."""
        ...

    async def save(self, state: InstanceState) -> None: ...


class AuditLog(Protocol):
    async def record(self, entry: AuditEntry) -> None: ...


# --- secrets at rest (ADR-016) ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    """AEAD output; the bytes never appear in ``repr`` (and so never in logs or tracebacks)."""

    key_id: str
    algorithm: str
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)


class SecretDecryptionError(Exception):
    """Unknown key, corrupted ciphertext or mismatched associated data. Never carries the value."""


class SecretCipher(Protocol):
    """Encrypts secrets bound to their record: ``secret_id`` and ``kind`` are authenticated, not encrypted."""

    @property
    def primary_key_id(self) -> str: ...

    @property
    def key_ids(self) -> frozenset[str]: ...

    def encrypt(self, secret_id: UUID, kind: SecretKind, value: SecretValue) -> EncryptedSecret:
        """Always with the primary key and a fresh random nonce."""
        ...

    def decrypt(self, secret_id: UUID, kind: SecretKind, encrypted: EncryptedSecret) -> SecretValue:
        """Raises :class:`SecretDecryptionError`."""
        ...


@dataclass(frozen=True, slots=True)
class SecretRecord:
    id: UUID
    kind: SecretKind
    encrypted: EncryptedSecret
    hint: str | None


class SecretRepository(Protocol):
    async def add(self, record: SecretRecord, *, at: datetime) -> None: ...

    async def get(self, secret_id: UUID) -> SecretRecord | None: ...

    async def get_for_update(self, secret_id: UUID) -> SecretRecord | None:
        """Locks the row until the end of the transaction."""
        ...

    async def replace(
        self, secret_id: UUID, encrypted: EncryptedSecret, *, hint: str | None, at: datetime
    ) -> None:
        """A new value for the same record (the old ciphertext is overwritten)."""
        ...

    async def reencrypt(self, secret_id: UUID, encrypted: EncryptedSecret, *, at: datetime) -> None:
        """The same value under another key (rotation); sets ``rotated_at``."""
        ...

    async def delete(self, secret_id: UUID) -> None: ...

    async def count_by_key_id(self) -> dict[str, int]: ...

    async def ids_not_on_key(self, key_id: str, *, after: UUID | None, limit: int) -> list[UUID]:
        """Ids of records encrypted with any other key, ordered by id, strictly after ``after``."""
        ...


class ProviderRepository(Protocol):
    """Non-deleted AI providers. ``secret_hint`` is read from the referenced secret."""

    async def add(self, provider: AIProvider) -> None:
        """Raises :class:`UniqueViolationError` for a taken name."""
        ...

    async def get(self, provider_id: UUID) -> AIProvider | None: ...

    async def get_for_update(self, provider_id: UUID) -> AIProvider | None: ...

    async def get_by_name(self, name: str) -> AIProvider | None: ...

    async def page(self, page: PageRequest) -> Page[AIProvider]: ...

    async def update(self, provider: AIProvider, *, at: datetime) -> None:
        """Writes every mutable column, including the last test result and ``deleted_at``."""
        ...

    async def record_test(
        self, provider_id: UUID, *, at: datetime, status: TestStatus, error_code: str | None
    ) -> None: ...


class ModelConfigRepository(Protocol):
    async def list_for_provider(self, provider_id: UUID) -> list[ModelConfig]: ...

    async def get(self, provider_id: UUID, config_id: UUID) -> ModelConfig | None: ...

    async def add(self, model: ModelConfig) -> None:
        """Raises :class:`UniqueViolationError` for a model ID the provider already has."""
        ...

    async def update(self, model: ModelConfig, *, at: datetime) -> None: ...

    async def delete(self, config_id: UUID) -> None: ...

    async def delete_for_provider(self, provider_id: UUID) -> int: ...


class EmbeddingProfileRepository(Protocol):
    async def page(self, page: PageRequest) -> Page[EmbeddingProfile]: ...

    async def get(self, profile_id: UUID) -> EmbeddingProfile | None: ...

    async def get_by_name(self, name: str) -> EmbeddingProfile | None: ...

    async def add(self, profile: EmbeddingProfile) -> None:
        """Raises :class:`UniqueViolationError` for a taken name."""
        ...

    async def update(self, profile: EmbeddingProfile, *, at: datetime) -> None:
        """Only ``name`` and ``status`` are mutable."""
        ...

    async def count_for_provider(self, provider_id: UUID) -> int: ...


@dataclass(frozen=True, slots=True)
class IndexingStats:
    """Live documents of a knowledge base by processing status (API_CONTRACTS.md §4)."""

    documents_total: int = 0
    indexed: int = 0
    processing: int = 0  # pending or processing
    failed: int = 0


class KnowledgeBaseRepository(Protocol):
    """Non-deleted knowledge bases and their grants."""

    async def add(self, kb: KnowledgeBase) -> None:
        """Raises :class:`UniqueViolationError` for a taken name."""
        ...

    async def get(self, kb_id: UUID) -> KnowledgeBase | None: ...

    async def get_by_name(self, name: str) -> KnowledgeBase | None: ...

    async def page(self, page: PageRequest, *, visible_to: UUID | None) -> Page[KnowledgeBase]:
        """All knowledge bases with ``visible_to=None``; otherwise those granted to the user directly or
        through one of their non-deleted groups."""
        ...

    async def grants_of_user(self, user_id: UUID, kb_ids: list[UUID]) -> dict[UUID, list[KbAccessLevel]]:
        """Direct and group grants of the user, per knowledge base (absent: no grant)."""
        ...

    async def indexing_stats(self, kb_ids: list[UUID]) -> dict[UUID, IndexingStats]: ...

    async def linked_to_source(self, source_id: UUID) -> list[KnowledgeBase]:
        """Live knowledge bases that include the document source."""
        ...


class DataSourceRepository(Protocol):
    async def add(self, source: DataSource) -> None: ...

    async def attach(self, kb_id: UUID, source_id: UUID, *, at: datetime) -> None: ...

    async def upload_source_of(self, kb_id: UUID) -> DataSource | None: ...

    async def get(self, source_id: UUID) -> DataSource | None:
        """Including deleted sources (callers check ``deleted_at``)."""
        ...

    async def linked_to(self, kb_id: UUID) -> list[DataSource]:
        """The non-deleted sources included in the knowledge base."""
        ...


@dataclass(frozen=True, slots=True)
class DocumentQuery:
    """``GET /documents``: the sources of one knowledge base (resolved by the use case) and allowlisted
    filters. Deleted documents are never listed."""

    source_ids: tuple[UUID, ...]
    status: DocumentStatus | None = None
    search: str | None = None


@dataclass(frozen=True, slots=True)
class JobQuery:
    """``GET /jobs`` filters; ``source_ids`` limits jobs to documents of these sources."""

    status: JobStatus | None = None
    source_ids: tuple[UUID, ...] | None = None


class DocumentRepository(Protocol):
    async def add(self, document: Document) -> None: ...

    async def get(self, document_id: UUID) -> Document | None:
        """Including deleted documents (callers check ``deleted_at``)."""
        ...

    async def get_for_update(self, document_id: UUID) -> Document | None:
        """Locks the row: deletion, reindex and retry of one document are serialized."""
        ...

    async def page(self, query: DocumentQuery, page: PageRequest) -> Page[Document]: ...

    async def versions(self, document_id: UUID) -> list[DocumentVersion]:
        """Newest first."""
        ...

    async def latest_versions(self, document_ids: Sequence[UUID]) -> dict[UUID, DocumentVersion]:
        """The highest-numbered version of each document."""
        ...

    async def mark_deleted(self, document_id: UUID, reason: DeletionReason, *, at: datetime) -> None:
        """Soft delete: the document leaves retrieval and listings at once (§8)."""
        ...

    async def reset_version(self, version_id: UUID, *, at: datetime) -> None:
        """A ``failed`` version becomes ``pending`` again for a retry or reindex (other states unchanged)."""
        ...

    async def deleted_unpurged(self, *, deleted_before: datetime, limit: int) -> list[UUID]: ...

    async def mark_purged(self, document_id: UUID, *, at: datetime) -> None:
        """``purged_at`` on the document; its versions become ``deleted`` without an active chunk set."""
        ...

    async def add_version(self, version: DocumentVersion) -> None: ...

    async def get_version(self, version_id: UUID) -> DocumentVersion | None: ...

    async def set_status(self, document_id: UUID, status: DocumentStatus, *, at: datetime) -> None: ...

    async def set_mime_type(
        self, document_id: UUID, version_id: UUID, mime_type: str, *, at: datetime
    ) -> None:
        """The type confirmed from the whole file, on the document and its version."""
        ...

    async def fail_version(
        self, version_id: UUID, *, error_code: str, error_message: str, at: datetime
    ) -> None: ...

    async def record_extraction(
        self,
        version_id: UUID,
        *,
        extracted_storage_key: str,
        parser: str,
        page_count: int | None,
        at: datetime,
    ) -> None:
        """Stage 7 result: where the normalized document is and which parser produced it."""
        ...

    async def activate_version(
        self, document_id: UUID, version_id: UUID, *, chunk_set: int, at: datetime
    ) -> UUID | None:
        """Stage 11, in the caller's transaction: the version becomes ``ready`` with ``chunk_set`` active and
        the document's current version; the previous current version becomes ``outdated`` (returned)."""
        ...


@dataclass(frozen=True, slots=True)
class ChunkToEmbed:
    id: UUID
    content: str


@dataclass(frozen=True, slots=True)
class NewEmbedding:
    chunk_id: UUID
    vector: tuple[float, ...]


class ChunkRepository(Protocol):
    """Chunks (stage 8) and their vectors (stage 9). Inserts ignore rows that already exist: a retried or
    concurrent run of the same job never duplicates them (SC-09)."""

    async def chunk_sets(self, version_id: UUID) -> list[int]:
        """Distinct ``chunk_set`` numbers of the version, ascending."""
        ...

    async def add_chunks(
        self,
        *,
        document_id: UUID,
        version_id: UUID,
        data_source_id: UUID,
        chunk_set: int,
        drafts: Sequence[ChunkDraft],
    ) -> None: ...

    async def count(self, version_id: UUID, chunk_set: int) -> int: ...

    async def without_embedding(
        self, version_id: UUID, chunk_set: int, profile_id: UUID, *, limit: int
    ) -> list[ChunkToEmbed]:
        """The next chunks of the set (by ``chunk_index``) that have no vector for the profile."""
        ...

    async def add_embeddings(
        self,
        *,
        profile_id: UUID,
        version_id: UUID,
        data_source_id: UUID,
        dimensions: int,
        embeddings: Sequence[NewEmbedding],
    ) -> None: ...

    async def embedding_count(self, version_id: UUID, chunk_set: int, profile_id: UUID) -> int: ...

    async def delete_for_document(self, document_id: UUID) -> int:
        """All chunks of the document; their vectors go with them (``ON DELETE CASCADE``)."""
        ...


class ChunkSearch(Protocol):
    """The two retrieval branches (ARCHITECTURE.md §9.4). Every query carries all filters of
    DATABASE_SCHEMA.md §5.11 in SQL: the scope's sources, a live document, its current ``ready`` version and
    the active ``chunk_set``; the vector branch also the scope's profile (ADR-017 §3)."""

    async def by_vector(
        self, scope: RetrievalScope, vector: Sequence[float], *, limit: int
    ) -> list[ChunkHit]:
        """Nearest chunks first; ``score`` is the similarity (higher is closer)."""
        ...

    async def by_keywords(self, scope: RetrievalScope, query: str, *, limit: int) -> list[ChunkHit] | None:
        """Full-text matches, best first (ADR-018); ``None`` when the query has no searchable words."""
        ...


class VectorIndexManager(Protocol):
    """Per-profile partial HNSW indexes (ADR-017 §2). DDL is built only from the stored profile."""

    async def is_ready(self, profile: EmbeddingProfile) -> bool: ...

    async def ensure(self, profile: EmbeddingProfile) -> bool:
        """Creates the index (``CONCURRENTLY``), replacing an invalid one; ``True`` if it was created."""
        ...

    async def drop(self, profile_id: UUID) -> bool: ...


class IngestionJobRepository(Protocol):
    async def add(self, job: IngestionJob) -> None:
        """Raises :class:`UniqueViolationError` for a taken idempotency key."""
        ...

    async def get(self, job_id: UUID) -> IngestionJob | None: ...

    async def get_for_update(self, job_id: UUID) -> IngestionJob | None:
        """Locks the row: two deliveries of one job never run its stages at the same time."""
        ...

    async def save(self, job: IngestionJob) -> None:
        """Writes status, stage, result, attempt, error and timestamps."""
        ...

    async def set_task_id(self, job_id: UUID, task_id: str | None) -> None:
        """``None`` while the job waits to be published: the maintenance sweep sends it again."""
        ...

    async def unfinished_for_document(self, document_id: UUID) -> list[IngestionJob]:
        """``queued`` and ``running`` jobs of the document, locked."""
        ...

    async def page(self, query: JobQuery, page: PageRequest) -> Page[IngestionJob]: ...

    async def unpublished(self, *, idle_since: datetime, limit: int) -> list[IngestionJob]:
        """``queued`` jobs without a task ID, unchanged since ``idle_since``."""
        ...


class JobQueueUnavailableError(Exception):
    """The broker is unreachable; the job stays ``queued`` in the database and is sent again later."""


class JobQueue(Protocol):
    """Publishes job IDs to the workers. Messages carry only the ID: the worker reloads and re-checks the
    state (ADR-008, Security considerations)."""

    async def enqueue_ingestion(self, job_id: UUID) -> str:
        """Stages 2-7 on the ``ingestion`` queue (no network). Returns the broker task ID.

        Raises :class:`JobQueueUnavailableError` (all methods)."""
        ...

    async def enqueue_indexing(self, job_id: UUID) -> str:
        """Stages 8-12 on the ``embeddings`` queue (the worker that may reach embedding providers)."""
        ...

    async def enqueue_vector_index(self, profile_id: UUID) -> str:
        """``ensure_vector_index`` on the ``maintenance`` queue (ADR-017 §2)."""
        ...

    async def enqueue_purge(self, document_id: UUID) -> str:
        """``purge_document`` on the ``maintenance`` queue: derived data and files of a deleted document."""
        ...


class UnitOfWork(Protocol):
    """One database transaction. Leaving the context without ``commit()`` rolls back."""

    users: UserRepository
    roles: RoleRepository
    groups: GroupRepository
    activations: ActivationTokenRepository
    sessions: SessionRepository
    instance: InstanceStateRepository
    secrets: SecretRepository
    providers: ProviderRepository
    models: ModelConfigRepository
    embedding_profiles: EmbeddingProfileRepository
    knowledge_bases: KnowledgeBaseRepository
    data_sources: DataSourceRepository
    documents: DocumentRepository
    ingestion_jobs: IngestionJobRepository
    chunks: ChunkRepository
    search: ChunkSearch
    audit: AuditLog

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]
