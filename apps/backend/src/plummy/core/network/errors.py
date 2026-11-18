"""Errors of the outbound network policy. Messages never contain credentials, paths or query strings."""

from __future__ import annotations

from enum import StrEnum


class EgressDenialReason(StrEnum):
    INVALID_URL = "invalid_url"
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"  # not http(s), or plain http without an allowlist entry
    ADDRESS_NOT_ALLOWED = "address_not_allowed"  # internal address without a matching allowlist entry
    ADDRESS_NEVER_ALLOWED = "address_never_allowed"  # link-local/metadata/multicast/unspecified/platform
    UNSUPPORTED_TRANSPORT = "unsupported_transport"


class EgressDeniedError(Exception):
    """The connection is refused by the network policy (``egress_denied``)."""

    code = "egress_denied"

    def __init__(self, reason: EgressDenialReason, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason
        self.detail = detail


class ResponseTooLargeError(Exception):
    """The response body exceeded the configured limit."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"response exceeded {limit} bytes")
        self.limit = limit
