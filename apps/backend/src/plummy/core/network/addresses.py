"""IP address classes for outbound connections (ADR-007 §3), based on the IANA special-purpose registries.

Addresses that embed an IPv4 address (IPv4-mapped/compatible, NAT64, 6to4, Teredo) are unwrapped: the embedded
address is checked with the same rules as the outer one.
"""

from __future__ import annotations

from enum import StrEnum
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_network
from typing import Final

type IPAddress = IPv4Address | IPv6Address
type IPNetwork = IPv4Network | IPv6Network


class AddressClass(StrEnum):
    PUBLIC = "public"
    LOOPBACK = "loopback"
    PRIVATE = "private"
    SPECIAL = "special"  # CGNAT, benchmarking, documentation, reserved and other non-global ranges
    NEVER = "never"  # link-local, cloud metadata, unspecified, multicast, broadcast: never allowlisted


def _networks(*cidrs: str) -> tuple[IPNetwork, ...]:
    return tuple(ip_network(cidr) for cidr in cidrs)


# Never reachable, not even through the allowlist.
NEVER_NETWORKS: Final = _networks(
    "0.0.0.0/8",  # "this network", unspecified
    "169.254.0.0/16",  # link-local, includes 169.254.169.254 (cloud metadata)
    "100.100.100.200/32",  # Alibaba Cloud metadata
    "224.0.0.0/4",  # multicast
    "255.255.255.255/32",  # limited broadcast
    "::/128",  # unspecified
    "fe80::/10",  # link-local
    "fec0::/10",  # site-local (deprecated)
    "ff00::/8",  # multicast
    "fd00:ec2::254/128",  # AWS metadata over IPv6
)
LOOPBACK_NETWORKS: Final = _networks("127.0.0.0/8", "::1/128")
PRIVATE_NETWORKS: Final = _networks("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
# Not globally routable (IANA special-purpose registries); reachable only through an explicit allowlist CIDR.
SPECIAL_NETWORKS: Final = _networks(
    "100.64.0.0/10",  # CGNAT (shared address space)
    "192.0.0.0/24",  # IETF protocol assignments
    "192.0.2.0/24",  # TEST-NET-1
    "192.88.99.0/24",  # 6to4 relay anycast (deprecated)
    "198.18.0.0/15",  # benchmarking
    "198.51.100.0/24",  # TEST-NET-2
    "203.0.113.0/24",  # TEST-NET-3
    "240.0.0.0/4",  # reserved
    "::ffff:0:0/96",  # IPv4-mapped (unwrapped before classification)
    "64:ff9b::/96",  # NAT64 well-known prefix
    "64:ff9b:1::/48",  # NAT64 local-use
    "100::/64",  # discard-only
    "2001::/23",  # IETF protocol assignments (includes Teredo 2001::/32)
    "2001:db8::/32",  # documentation
    "2002::/16",  # 6to4
    "3fff::/20",  # documentation
    "5f00::/16",  # SRv6 SIDs
)

_NAT64_PREFIXES: Final = _networks("64:ff9b::/96", "64:ff9b:1::/48")
_IPV4_COMPATIBLE: Final = ip_network("::/96")


def _contained(address: IPAddress, networks: tuple[IPNetwork, ...]) -> bool:
    return any(address.version == network.version and address in network for network in networks)


def embedded_ipv4(address: IPAddress) -> IPv4Address | None:
    """The IPv4 address carried inside an IPv6 address, if any."""
    if isinstance(address, IPv4Address):
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.teredo is not None:
        return address.teredo[1]  # the client address (the server address is a relay)
    if address.sixtofour is not None:
        return address.sixtofour
    packed = address.packed
    if _contained(address, _NAT64_PREFIXES):
        return IPv4Address(packed[-4:])
    if address in _IPV4_COMPATIBLE and int(address) > 1:  # deprecated ::a.b.c.d (excluding :: and ::1)
        return IPv4Address(packed[-4:])
    return None


def unwrap(address: IPAddress) -> tuple[IPAddress, ...]:
    """The address and every address embedded in it; all of them must pass the policy."""
    inner = embedded_ipv4(address)
    return (address,) if inner is None else (address, inner)


def classify(address: IPAddress) -> AddressClass:
    """Class of a single address (without unwrapping)."""
    if _contained(address, NEVER_NETWORKS):
        return AddressClass.NEVER
    if _contained(address, LOOPBACK_NETWORKS):
        return AddressClass.LOOPBACK
    if _contained(address, PRIVATE_NETWORKS):
        return AddressClass.PRIVATE
    if _contained(address, SPECIAL_NETWORKS) or not address.is_global:
        return AddressClass.SPECIAL
    return AddressClass.PUBLIC


def is_never_allowed(address: IPAddress) -> bool:
    return any(classify(candidate) is AddressClass.NEVER for candidate in unwrap(address))


def is_public(address: IPAddress) -> bool:
    return all(classify(candidate) is AddressClass.PUBLIC for candidate in unwrap(address))


def overlaps_never(network: IPNetwork) -> bool:
    return any(network.version == never.version and network.overlaps(never) for never in NEVER_NETWORKS)
