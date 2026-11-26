"""Ports of external sources (ARCHITECTURE.md §8.6, ADR-020): the connector of a provider, its OAuth
client, the store of pending authorizations and the typed connector errors. Adapters live in
``infrastructure``; nothing here knows Google or HTTP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Protocol
from uuid import UUID

from plummy.domain.knowledge import DataSourceType
from plummy.domain.secrets import SecretValue


class ConnectorError(Exception):
    """A typed failure of a connector call (ADR-020 §9). The message is a safe summary: never a provider
    response body, a file name or a token."""

    code: ClassVar[str] = "connector_error"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class ConnectorAuthRevokedError(ConnectorError):
    """The provider refused the refresh token (revoked, expired or the consent was withdrawn)."""

    code = "connector_auth_revoked"


class ConnectorRateLimitedError(ConnectorError):
    code = "connector_rate_limited"
    retryable = True

    def __init__(self, retry_after_seconds: float | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after_seconds = retry_after_seconds


class ConnectorQuotaExceededError(ConnectorError):
    code = "connector_quota_exceeded"


class ConnectorNotFoundError(ConnectorError):
    code = "connector_not_found"


class ConnectorPermissionDeniedError(ConnectorError):
    code = "connector_permission_denied"


class ConnectorTransientError(ConnectorError):
    """Network failure, timeout or a 5xx answer: worth retrying later."""

    code = "connector_unavailable"
    retryable = True


class ConnectorCursorInvalidError(ConnectorError):
    """The saved change cursor is no longer accepted: a reconciliation is needed."""

    code = "connector_cursor_invalid"


class ConnectorAccessTokenRejectedError(ConnectorError):
    """The API refused a fresh access token (``401``): refresh once more before giving up."""

    code = "connector_access_token_rejected"
    retryable = True


class ConnectorFileTooLargeError(ConnectorError):
    """The file (or its export) is larger than the upload limit or the provider's export limit."""

    code = "connector_file_too_large"


class ConnectorEgressDeniedError(ConnectorError):
    """The network policy refused the connection (ADR-007): a configuration problem, not retried."""

    code = "egress_denied"


class ConnectorInvalidResponseError(ConnectorError):
    """The provider answered with something that is not the expected shape (or too large)."""

    code = "connector_invalid_response"


class OAuthExchangeError(ConnectorError):
    """The authorization code (or refresh) was refused for a reason other than a revoked grant."""

    code = "oauth_exchange_failed"


@dataclass(frozen=True, slots=True)
class OAuthGrant:
    """Tokens from the token endpoint. Only ``refresh_token`` is ever stored, encrypted (ADR-020 §7)."""

    access_token: SecretValue
    refresh_token: SecretValue | None
    granted_scopes: tuple[str, ...]
    expires_in_seconds: int


class OAuthClient(Protocol):
    """Authorization Code + PKCE (S256) against one provider."""

    @property
    def source_type(self) -> DataSourceType: ...

    @property
    def required_scopes(self) -> tuple[str, ...]: ...

    def has_required_scopes(self, granted: Sequence[str]) -> bool:
        """Whether the scopes the provider actually granted cover :attr:`required_scopes` (providers spell
        granted scopes their own way)."""
        ...

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str: ...

    async def exchange_code(
        self, code: SecretValue, *, code_verifier: SecretValue, redirect_uri: str
    ) -> OAuthGrant: ...

    async def refresh(self, refresh_token: SecretValue) -> OAuthGrant: ...

    async def revoke(self, token: SecretValue) -> bool:
        """Revokes the token at the provider; ``False`` when the provider has no way to revoke one token
        (Microsoft, ADR-021 §3): the caller then only removes it locally."""
        ...

    async def account_label(self, access_token: SecretValue) -> str | None:
        """Who granted the access, for the UI (personal data: never written to logs or audit)."""
        ...


OAuthClients = Mapping[DataSourceType, OAuthClient]  # configured providers only


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    """Server-side state of one OAuth start, keyed by the hash of ``state`` (ADR-020 §1)."""

    data_source_id: UUID
    user_id: UUID
    session_id: UUID
    code_verifier: str


class OAuthStateUnavailableError(Exception):
    """The state store (Redis) is unavailable."""


class OAuthStateStore(Protocol):
    async def put(self, key: str, pending: PendingAuthorization, *, ttl_seconds: int) -> None: ...

    async def take(self, key: str) -> PendingAuthorization | None:
        """Returns and deletes the entry in one step: a state is usable once."""
        ...


# --- the connector -------------------------------------------------------------------------------------


class RemoteKind(StrEnum):
    FOLDER = "folder"
    FILE = "file"  # downloadable as is or through an export
    SHORTCUT = "shortcut"  # never followed: it could lead out of the chosen folder (ADR-020 §8)
    UNSUPPORTED = "unsupported"  # forms, sites, maps and other formats without an export


@dataclass(frozen=True, slots=True)
class RemoteItem:
    """A file or folder of the provider. ``revision`` changes exactly when the content does (a checksum, or
    a version for formats that are exported); ``filename`` and ``content_type`` are what a download gives.
    Names come from the provider: untrusted, never used as storage paths."""

    id: str
    name: str
    kind: RemoteKind
    mime_type: str
    parents: tuple[str, ...]
    trashed: bool
    size: int | None
    revision: str | None
    modified_at: datetime | None
    drive_id: str | None
    filename: str
    content_type: str


@dataclass(frozen=True, slots=True)
class RemotePage:
    items: tuple[RemoteItem, ...]
    next_page_token: str | None


class BrowseKind(StrEnum):
    FOLDER = "folder"
    SHARED_DRIVE = "shared_drive"
    SITE = "site"  # a SharePoint site: opened to list its libraries, never chosen (ADR-021 §6)
    DRIVE = "drive"  # a document library or a OneDrive: its root can be chosen as the folder


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """Where a source syncs (ADR-021 §8): the chosen folder and, for providers whose change feed belongs to
    one drive (Microsoft Graph), that drive. Google's feed covers the account and ignores ``drive_id``."""

    folder_id: str
    drive_id: str | None = None


@dataclass(frozen=True, slots=True)
class BrowseEntry:
    id: str
    name: str
    kind: BrowseKind


@dataclass(frozen=True, slots=True)
class BrowsePage:
    entries: tuple[BrowseEntry, ...]
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class RemoteChange:
    """``item`` is ``None`` when the file was removed or is no longer visible to the account."""

    item_id: str
    removed: bool
    item: RemoteItem | None


@dataclass(frozen=True, slots=True)
class ChangesPage:
    """``cursor`` continues the listing while ``finished`` is false and is the start of the next sync once
    it is true; saving it after each page is the checkpoint (ADR-020 §8)."""

    changes: tuple[RemoteChange, ...]
    cursor: str
    finished: bool


@dataclass(frozen=True, slots=True)
class ConnectorCapabilities:
    incremental_changes: bool
    folder_picker: bool
    revoke: bool
    exports: Mapping[str, str]  # provider format -> the format it is exported to


class DataSourceConnector(Protocol):
    """Reads one provider with an access token obtained by the caller (never stored). Every call is
    bounded (pagination, size limits); failures are :class:`ConnectorError` subclasses."""

    @property
    def source_type(self) -> DataSourceType: ...

    def capabilities(self) -> ConnectorCapabilities: ...

    async def browse(
        self, access_token: SecretValue, parent_id: str | None, *, page_token: str | None
    ) -> BrowsePage:
        """Folders under ``parent_id``; ``None`` is the top level (own folders, folders shared with the
        account, shared drives)."""
        ...

    async def get_item(self, access_token: SecretValue, item_id: str) -> RemoteItem: ...

    async def list_children(
        self, access_token: SecretValue, folder_id: str, *, page_token: str | None
    ) -> RemotePage:
        """One page of the direct, non-trashed children of a folder (recursion belongs to the caller)."""
        ...

    async def start_cursor(self, access_token: SecretValue, location: SourceLocation) -> str:
        """The change cursor of "now", taken before a full listing."""
        ...

    async def get_changes(
        self, access_token: SecretValue, location: SourceLocation, cursor: str
    ) -> ChangesPage: ...

    def download(
        self, access_token: SecretValue, item: RemoteItem, *, max_bytes: int
    ) -> AsyncIterator[bytes]:
        """The content (or the export) of a ``FILE``, at most ``max_bytes`` (else
        :class:`ConnectorFileTooLargeError`)."""
        ...


Connectors = Mapping[DataSourceType, DataSourceConnector]  # configured providers only
