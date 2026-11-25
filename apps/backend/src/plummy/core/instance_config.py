"""Instance configuration file (``CONFIG_FILE``, TOML): settings that are too structured for environment
variables. Like all Instance Settings it is read at startup and never changed through the API (ADR-006).

Currently only the outbound network allowlist (ADR-007)::

    [[network.allowlist]]
    name           = "local-ollama"
    url            = "http://ollama:11434"
    purposes       = ["llm", "embedding"]
    provider_types = ["ollama"]                 # optional
    allowed_cidrs  = ["172.30.10.0/24"]         # required unless the address is public
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import ip_network
from pathlib import Path
from typing import Any, Final

from plummy.core.config import ConfigurationError
from plummy.core.network.addresses import IPAddress, IPNetwork, is_public, overlaps_never
from plummy.core.network.errors import EgressDeniedError
from plummy.core.network.policy import AllowlistEntry, EgressPolicy, EgressPurpose
from plummy.core.network.urls import parse_egress_url
from plummy.domain.ai import ProviderType
from plummy.domain.knowledge import DataSourceType

MAX_CONFIG_FILE_BYTES: Final = 256 * 1024
_NAME_RE: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_ENTRY_KEYS: Final = frozenset({"name", "url", "purposes", "provider_types", "allowed_cidrs"})


@dataclass(frozen=True, slots=True)
class InstanceConfig:
    allowlist: tuple[AllowlistEntry, ...] = ()


def _fail(message: str) -> ConfigurationError:
    return ConfigurationError(f"Invalid configuration: CONFIG_FILE: {message}")


def _string_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _fail(f"{where} must be a list of strings")
    if len(set(value)) != len(value):
        raise _fail(f"{where} has duplicates")
    return value


def _parse_entry(raw: Any, index: int) -> AllowlistEntry:
    where = f"network.allowlist[{index}]"
    if not isinstance(raw, dict):
        raise _fail(f"{where} must be a table")
    unknown = set(raw) - _ENTRY_KEYS
    if unknown:
        raise _fail(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise _fail(f"{where}.name must be 1-64 characters of [a-z0-9_-]")
    where = f"network.allowlist '{name}'"
    url_text = raw.get("url")
    if not isinstance(url_text, str):
        raise _fail(f"{where}: url is required")
    try:
        url = parse_egress_url(url_text)
    except EgressDeniedError as exc:
        raise _fail(f"{where}: url: {exc}") from None
    if "?" in url_text:
        raise _fail(f"{where}: url must not have a query")

    purposes_raw = _string_list(raw.get("purposes"), f"{where}: purposes")
    if not purposes_raw:
        raise _fail(f"{where}: purposes must not be empty")
    try:
        purposes = frozenset(EgressPurpose(item) for item in purposes_raw)
    except ValueError:
        raise _fail(f"{where}: purposes must be from {sorted(p.value for p in EgressPurpose)}") from None

    provider_types: frozenset[str] | None = None
    if "provider_types" in raw:
        types = _string_list(raw["provider_types"], f"{where}: provider_types")
        # AI provider types for llm/embedding entries, external source types for connector entries.
        known = {item.value for item in ProviderType} | {
            item.value for item in DataSourceType if item is not DataSourceType.UPLOAD
        }
        if not types or set(types) - known:
            raise _fail(f"{where}: provider_types must be a non-empty subset of {sorted(known)}")
        provider_types = frozenset(types)

    cidrs: list[IPNetwork] = []
    for text in _string_list(raw.get("allowed_cidrs", []), f"{where}: allowed_cidrs"):
        try:
            network = ip_network(text, strict=True)
        except ValueError:
            raise _fail(f"{where}: invalid CIDR {text!r} (host bits must be zero)") from None
        if overlaps_never(network):
            raise _fail(f"{where}: {text} overlaps link-local, metadata, multicast or unspecified addresses")
        cidrs.append(network)

    if (
        url.address is not None
        and not is_public(url.address)
        and not any(url.address.version == net.version and url.address in net for net in cidrs)
    ):
        raise _fail(f"{where}: allowed_cidrs must contain the internal address of the url")
    return AllowlistEntry(
        name=name, url=url, purposes=purposes, provider_types=provider_types, allowed_cidrs=tuple(cidrs)
    )


def parse_instance_config(data: bytes) -> InstanceConfig:
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _fail(f"not valid TOML ({exc})") from None
    unknown = set(document) - {"network"}
    if unknown:
        raise _fail(f"unknown sections: {', '.join(sorted(unknown))}")
    network = document.get("network", {})
    if not isinstance(network, dict) or set(network) - {"allowlist"}:
        raise _fail("[network] may contain only 'allowlist'")
    entries_raw = network.get("allowlist", [])
    if not isinstance(entries_raw, list):
        raise _fail("network.allowlist must be an array of tables")
    entries = tuple(_parse_entry(raw, index) for index, raw in enumerate(entries_raw))
    names = [entry.name for entry in entries]
    if len(set(names)) != len(names):
        raise _fail("network.allowlist names must be unique")
    return InstanceConfig(allowlist=entries)


def load_instance_config(path: Path | None) -> InstanceConfig:
    if path is None:
        return InstanceConfig()
    try:
        if path.stat().st_size > MAX_CONFIG_FILE_BYTES:
            raise _fail(f"{path} is larger than {MAX_CONFIG_FILE_BYTES} bytes")
        data = path.read_bytes()
    except OSError as exc:
        raise _fail(f"cannot read {path}: {exc.strerror or type(exc).__name__}") from None
    return parse_instance_config(data)


def build_egress_policy(
    config: InstanceConfig, platform_addresses: Iterable[tuple[str, IPAddress]] = ()
) -> EgressPolicy:
    """The policy for this process. An allowlist CIDR that contains an address of the platform's own services
    (PostgreSQL, Redis, storage) is a configuration error (ADR-007 §3)."""
    forbidden: set[IPAddress] = set()
    for service, address in platform_addresses:
        forbidden.add(address)
        for entry in config.allowlist:
            if any(address.version == net.version and address in net for net in entry.allowed_cidrs):
                raise _fail(f"network.allowlist '{entry.name}' includes the address of {service}")
    return EgressPolicy(allowlist=config.allowlist, forbidden_addresses=frozenset(forbidden))
