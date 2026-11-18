"""Email addresses: syntax validation and the canonical form used for uniqueness and login."""

from __future__ import annotations

from email_validator import EmailNotValidError, validate_email

MAX_EMAIL_LENGTH = 254


class InvalidEmailError(ValueError):
    """The value is not a syntactically valid email address. The message never contains the value."""


def normalize_email(value: str) -> str:
    """Validate syntax (no DNS lookups) and return the canonical lower-case form.

    Single-label intranet domains (``user@intranet``) are accepted: installations are self-hosted.
    Special-use names (``.local``, ``.localhost``, ``.test``, ``.invalid``, ``.onion``, ``.arpa``) are
    rejected by the validator.
    """
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_EMAIL_LENGTH:
        raise InvalidEmailError("Enter a valid email address.")
    try:
        result = validate_email(candidate, check_deliverability=False, globally_deliverable=False)
    except EmailNotValidError:
        raise InvalidEmailError("Enter a valid email address.") from None
    normalized = result.normalized.lower()
    if len(normalized) > MAX_EMAIL_LENGTH:
        raise InvalidEmailError("Enter a valid email address.")
    return normalized


def canonical_login_email(value: str) -> str:
    """Best-effort canonical form for login lookups and rate-limit keys; never raises.

    Login must not reveal whether an input is a valid address, so invalid input is only folded to
    lower case and then simply matches no account.
    """
    try:
        return normalize_email(value)
    except InvalidEmailError:
        return value.strip().lower()[:MAX_EMAIL_LENGTH]
