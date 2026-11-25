"""Application errors. Transport-agnostic: the API maps them to problem details (ADR-010)."""

from __future__ import annotations


class ApplicationError(Exception):
    """Base class. ``code`` is the stable machine-readable code; messages never contain secrets."""

    code: str = "bad_request"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.__class__.__doc__ or self.code)
        self.message = message


class AuthenticationRequiredError(ApplicationError):
    """No valid session: missing, unknown, expired, or the user can no longer sign in."""

    code = "unauthenticated"


class SessionRevokedError(AuthenticationRequiredError):
    """The session was revoked."""

    code = "session_revoked"


class InvalidCredentialsError(ApplicationError):
    """Same error for an unknown email, a wrong password and a disabled or deleted user."""

    code = "invalid_credentials"


class PermissionDeniedError(ApplicationError):
    """The user lacks a required permission."""

    code = "permission_denied"


class NotFoundError(ApplicationError):
    """The resource does not exist or is not visible to the caller."""

    code = "not_found"


class ConflictError(ApplicationError):
    """The operation conflicts with the current state."""

    code = "conflict"


class RateLimitedError(ApplicationError):
    """Too many attempts."""

    code = "rate_limited"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Too many attempts. Try again later.")
        self.retry_after_seconds = max(1, retry_after_seconds)


class PayloadTooLargeError(ApplicationError):
    """The upload exceeds the size limit."""

    code = "payload_too_large"


class UnsupportedMediaTypeError(ApplicationError):
    """The file is not one of the supported formats (decided by its content)."""

    code = "unsupported_media_type"


class ServiceUnavailableError(ApplicationError):
    """A required dependency (database, Redis) is unavailable."""

    code = "service_unavailable"


class SetupAlreadyCompletedError(ApplicationError):
    """The instance is already initialized."""

    code = "setup_already_completed"


class SetupTokenInvalidError(ApplicationError):
    """The bootstrap token is wrong, expired or already used."""

    code = "setup_token_invalid"


class WebSetupDisabledError(NotFoundError):
    """Web setup is disabled (SETUP_MODE=cli_only)."""


class InvalidInputError(ApplicationError):
    """A field failed a business rule (email, display name, password policy)."""

    code = "validation_error"
    location = "body"  # where ``field`` lives: ``body`` or ``query``

    def __init__(self, field: str, error_code: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.error_code = error_code


class InvalidQueryParameterError(InvalidInputError):
    """A query parameter is unknown or invalid (ADR-014)."""

    code = "invalid_query_parameter"
    location = "query"


class EmailTakenError(ConflictError):
    """Another user already has this email."""

    code = "email_taken"


class GroupNameTakenError(ConflictError):
    """Another group already has this name."""

    code = "group_name_taken"


class LastAdministratorError(ConflictError):
    """The change would leave the instance without an active administrator."""

    code = "last_administrator"


class SelfModificationError(ConflictError):
    """Administrators cannot disable or delete their own account."""

    code = "cannot_modify_self"


class InvalidUserStateError(ConflictError):
    """The operation is not allowed in the user's current state."""

    code = "invalid_user_state"


class ActivationTokenInvalidError(ApplicationError):
    """The activation link is invalid, expired or already used."""

    code = "activation_token_invalid"


class ProviderNameTakenError(ConflictError):
    """Another AI provider already has this name."""

    code = "provider_name_taken"


class ProviderInUseError(ConflictError):
    """The provider is still used by an embedding profile (later also by assistants)."""

    code = "provider_in_use"


class ModelIdTakenError(ConflictError):
    """The provider already has a model config with this model ID."""

    code = "model_id_taken"


class EmbeddingProfileNameTakenError(ConflictError):
    """Another embedding profile already has this name."""

    code = "embedding_profile_name_taken"


class KnowledgeBaseNameTakenError(ConflictError):
    """Another knowledge base already has this name."""

    code = "knowledge_base_name_taken"


class ReindexRequiredError(ConflictError):
    """Changing the model or vector space of an embedding profile needs a new profile and a reindex."""

    code = "reindex_required"


class InvalidStateError(ConflictError):
    """The operation is not allowed in the current state of the resource."""

    code = "invalid_state"


class EndpointNotAllowedError(InvalidInputError):
    """The provider ``base_url`` does not pass the instance network policy (ADR-007)."""

    code = "endpoint_not_allowed"

    def __init__(self, reason: str) -> None:
        super().__init__("base_url", reason, "Endpoint is not allowed by instance network policy.")


class UpstreamProviderError(ApplicationError):
    """An AI provider call needed by the request failed (``502 provider_error``). ``reason`` is the typed
    provider code for logs; the response carries no provider output."""

    code = "provider_error"

    def __init__(self, reason: str, message: str = "The AI provider request failed.") -> None:
        super().__init__(message)
        self.reason = reason


class UpstreamTimeoutError(UpstreamProviderError):
    code = "provider_timeout"


class UpstreamEgressDeniedError(UpstreamProviderError):
    code = "egress_denied"
