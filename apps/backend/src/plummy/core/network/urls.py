"""Strict parsing of outbound URLs (ADR-007 §4 steps 1-2).

Forms that parsers interpret differently are rejected instead of normalized: userinfo, backslashes,
non-canonical IPv4 literals (``2130706433``, ``0177.0.0.1``, ``0x7f.1``, ``127.1``), IPv6 zone ids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Final
from urllib.parse import urlsplit

from plummy.core.network.addresses import IPAddress
from plummy.core.network.errors import EgressDenialReason, EgressDeniedError

ALLOWED_SCHEMES: Final = frozenset({"http", "https"})
DEFAULT_PORTS: Final = {"http": 80, "https": 443}
MAX_URL_LENGTH: Final = 2048
_LABEL_RE: Final = re.compile(r"(?!-)[a-z0-9_-]{1,63}(?<!-)")
_NUMERIC_LABEL_RE: Final = re.compile(r"(0x[0-9a-f]*|[0-9]+)")
_CANONICAL_OCTET_RE: Final = re.compile(r"0|[1-9][0-9]{0,2}")
_FORBIDDEN_PATH_RE: Final = re.compile(r"(^|/)\.{1,2}(/|$)|%2e|%2f|%5c", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class EgressUrl:
    scheme: str
    host: str  # lowercase ASCII hostname, or the canonical text of an IP address (IPv6 without brackets)
    port: int
    path: str
    address: IPAddress | None  # set when the host is an IP literal

    @property
    def origin(self) -> str:
        host = f"[{self.host}]" if isinstance(self.address, IPv6Address) else self.host
        return f"{self.scheme}://{host}:{self.port}"


def _invalid(detail: str) -> EgressDeniedError:
    return EgressDeniedError(EgressDenialReason.INVALID_URL, detail)


def _parse_ipv4_literal(host: str) -> IPv4Address | None:
    """``None`` for a hostname; an error for any numeric form other than canonical dotted-quad."""
    labels = host.split(".")
    if not _NUMERIC_LABEL_RE.fullmatch(labels[-1]):
        return None  # a hostname: its last label is not numeric
    if len(labels) != 4 or not all(_CANONICAL_OCTET_RE.fullmatch(label) for label in labels):
        raise _invalid("IPv4 addresses must be written as four decimal octets")
    try:
        return IPv4Address(host)
    except ValueError:
        raise _invalid("invalid IPv4 address") from None


def _normalize_hostname(host: str) -> str:
    host = host[:-1] if host.endswith(".") else host
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise _invalid("invalid hostname") from None
    if not ascii_host or len(ascii_host) > 253:
        raise _invalid("invalid hostname")
    if not all(_LABEL_RE.fullmatch(label) for label in ascii_host.split(".")):
        raise _invalid("invalid hostname")
    return ascii_host


def parse_egress_url(url: str) -> EgressUrl:
    """Raises :class:`EgressDeniedError` (``invalid_url`` / ``scheme_not_allowed``)."""
    if not url or len(url) > MAX_URL_LENGTH:
        raise _invalid("empty or too long")
    if "\\" in url or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
        raise _invalid("backslashes, whitespace and control characters are not allowed")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise _invalid("unparsable URL") from None
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise EgressDeniedError(EgressDenialReason.SCHEME_NOT_ALLOWED, "only http and https are allowed")
    if parts.fragment or url.endswith("#"):
        raise _invalid("fragments are not allowed")
    netloc = parts.netloc
    if not netloc or "@" in netloc:
        raise _invalid("a host is required and credentials in the URL are not allowed")

    raw_host, port = _split_host_port(netloc)
    address: IPAddress | None
    if raw_host.startswith("["):
        if not raw_host.endswith("]") or "%" in raw_host:
            raise _invalid("invalid IPv6 literal (zone ids are not allowed)")
        try:
            address = IPv6Address(raw_host[1:-1])
        except ValueError:
            raise _invalid("invalid IPv6 literal") from None
        host = str(address)
    else:
        lowered = raw_host.lower()
        address = _parse_ipv4_literal(lowered.removesuffix("."))
        host = str(address) if address is not None else _normalize_hostname(raw_host)

    path = parts.path or "/"
    if not path.startswith("/") or _FORBIDDEN_PATH_RE.search(path):
        raise _invalid("dot segments and encoded separators are not allowed in the path")
    return EgressUrl(scheme=scheme, host=host, port=port or DEFAULT_PORTS[scheme], path=path, address=address)


def _split_host_port(netloc: str) -> tuple[str, int | None]:
    if netloc.startswith("["):
        end = netloc.find("]")
        if end == -1:
            raise _invalid("invalid IPv6 literal")
        host, rest = netloc[: end + 1], netloc[end + 1 :]
    else:
        host, separator, port_text = netloc.partition(":")
        rest = f":{port_text}" if separator else ""
    if not host:
        raise _invalid("a host is required")
    if not rest:
        return host, None
    if not rest.startswith(":") or not rest[1:].isdigit() or not rest[1:].isascii():
        raise _invalid("invalid port")
    port = int(rest[1:])
    if not 1 <= port <= 65535 or rest[1:] != str(port):
        raise _invalid("invalid port")
    return host, port


def parse_ip(value: str) -> IPAddress | None:
    try:
        return ip_address(value)
    except ValueError:
        return None
