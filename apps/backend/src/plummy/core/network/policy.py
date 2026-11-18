"""The outbound allowlist and the decision for a URL and its resolved addresses (ADR-007 §1, §4-5)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from plummy.core.network.addresses import (
    IPAddress,
    IPNetwork,
    is_never_allowed,
    is_public,
    unwrap,
)
from plummy.core.network.errors import EgressDenialReason, EgressDeniedError
from plummy.core.network.urls import EgressUrl, parse_egress_url


class EgressPurpose(StrEnum):
    LLM = "llm"
    EMBEDDING = "embedding"


@dataclass(frozen=True, slots=True)
class AllowlistEntry:
    """One ``[[network.allowlist]]`` record: exactly ``scheme://host:port`` with an optional path prefix."""

    name: str
    url: EgressUrl
    purposes: frozenset[EgressPurpose]
    provider_types: frozenset[str] | None = None  # None: any provider type
    allowed_cidrs: tuple[IPNetwork, ...] = ()  # empty: only public addresses

    @property
    def path_prefix(self) -> str:
        return "" if self.url.path == "/" else self.url.path

    def matches(self, url: EgressUrl, purpose: EgressPurpose, provider_type: str | None) -> bool:
        if (url.scheme, url.host, url.port) != (self.url.scheme, self.url.host, self.url.port):
            return False
        base = self.path_prefix.rstrip("/")
        if base and not (url.path == base or url.path.startswith(base + "/")):
            return False
        if purpose not in self.purposes:
            return False
        return self.provider_types is None or (
            provider_type is not None and provider_type in self.provider_types
        )


@dataclass(frozen=True, slots=True)
class EgressRule:
    """What a connection may reach. ``allowed_cidrs`` empty means public addresses only."""

    entry: str | None
    allowed_cidrs: tuple[IPNetwork, ...] = ()

    @property
    def public_only(self) -> bool:
        return not self.allowed_cidrs


PUBLIC_RULE = EgressRule(entry=None)


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    allowlist: tuple[AllowlistEntry, ...] = ()
    # Addresses of the platform's own services (PostgreSQL, Redis, storage): never reachable.
    forbidden_addresses: frozenset[IPAddress] = field(default_factory=frozenset)

    def rule_for(self, url: EgressUrl, purpose: EgressPurpose, provider_type: str | None) -> EgressRule:
        """Static part of the decision (no DNS). Raises :class:`EgressDeniedError`."""
        matching = [entry for entry in self.allowlist if entry.matches(url, purpose, provider_type)]
        if matching:
            entry = max(matching, key=lambda candidate: len(candidate.path_prefix))
            rule = EgressRule(entry=entry.name, allowed_cidrs=entry.allowed_cidrs)
        elif url.scheme != "https":
            raise EgressDeniedError(
                EgressDenialReason.SCHEME_NOT_ALLOWED, "plain http needs an allowlist entry"
            )
        else:
            rule = PUBLIC_RULE
        if url.address is not None:
            self.check_address(rule, url.address)
        return rule

    def check_url(self, url: str, purpose: EgressPurpose, provider_type: str | None) -> EgressRule:
        """Validation of a configured endpoint (e.g. a provider ``base_url``) before any connection."""
        return self.rule_for(parse_egress_url(url), purpose, provider_type)

    def check_address(self, rule: EgressRule, address: IPAddress) -> None:
        """Dynamic part, for every resolved address of every connection."""
        candidates = unwrap(address)
        if is_never_allowed(address) or any(item in self.forbidden_addresses for item in candidates):
            raise EgressDeniedError(EgressDenialReason.ADDRESS_NEVER_ALLOWED)
        if rule.public_only:
            if not is_public(address):
                raise EgressDeniedError(EgressDenialReason.ADDRESS_NOT_ALLOWED)
            return
        for candidate in candidates:
            if not any(
                candidate.version == network.version and candidate in network
                for network in rule.allowed_cidrs
            ):
                raise EgressDeniedError(EgressDenialReason.ADDRESS_NOT_ALLOWED)

    def check_addresses(self, rule: EgressRule, addresses: Iterable[IPAddress]) -> None:
        """All addresses must pass: a name that resolves to one public and one internal address is refused."""
        for address in addresses:
            self.check_address(rule, address)
