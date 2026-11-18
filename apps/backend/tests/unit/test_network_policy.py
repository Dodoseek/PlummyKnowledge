"""Outbound network policy (ADR-007): URL parsing, address classes, allowlist decisions, CONFIG_FILE."""

from __future__ import annotations

from ipaddress import IPv4Address, ip_address, ip_network
from pathlib import Path

import pytest

from plummy.core.config import ConfigurationError
from plummy.core.instance_config import build_egress_policy, load_instance_config, parse_instance_config
from plummy.core.network.addresses import AddressClass, classify, embedded_ipv4, is_never_allowed, is_public
from plummy.core.network.errors import EgressDenialReason, EgressDeniedError
from plummy.core.network.policy import PUBLIC_RULE, EgressPolicy, EgressPurpose, EgressRule
from plummy.core.network.urls import parse_egress_url

LLM = EgressPurpose.LLM
EMBEDDING = EgressPurpose.EMBEDDING

OLLAMA_CONFIG = b"""
[[network.allowlist]]
name           = "local-ollama"
url            = "http://ollama:11434"
purposes       = ["llm", "embedding"]
provider_types = ["ollama"]
allowed_cidrs  = ["172.30.10.0/24"]

[[network.allowlist]]
name     = "gateway"
url      = "https://llm.example.com/v1/"
purposes = ["llm"]
"""


# --- URL parsing -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "scheme", "host", "port", "path"),
    [
        ("https://api.openai.com/v1", "https", "api.openai.com", 443, "/v1"),
        ("HTTPS://API.OpenAI.com.", "https", "api.openai.com", 443, "/"),
        ("http://ollama:11434", "http", "ollama", 11434, "/"),
        ("http://host.docker.internal:11434/api", "http", "host.docker.internal", 11434, "/api"),
        ("https://1.1.1.1/x?y=1", "https", "1.1.1.1", 443, "/x"),
        ("https://[2606:4700:4700::1111]:8443/", "https", "2606:4700:4700::1111", 8443, "/"),
        ("https://xn--d1acufc.xn--p1ai/", "https", "xn--d1acufc.xn--p1ai", 443, "/"),
        ("https://домен.рф/", "https", "xn--d1acufc.xn--p1ai", 443, "/"),
        ("http://my_service:8080", "http", "my_service", 8080, "/"),
    ],
)
def test_valid_urls_are_normalized(url: str, scheme: str, host: str, port: int, path: str) -> None:
    parsed = parse_egress_url(url)
    assert (parsed.scheme, parsed.host, parsed.port, parsed.path) == (scheme, host, port, path)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://user:pass@api.openai.com/",  # userinfo
        "https://api.openai.com@127.0.0.1/",
        "https://api.openai.com\\@127.0.0.1/",  # backslash
        "https://api.openai.com /",
        "https://api.\x00openai.com/",
        "https:///path",  # no host
        "https://",
        "https://2130706433/",  # decimal IPv4
        "https://0177.0.0.1/",  # octal
        "https://0x7f.0.0.1/",  # hex
        "https://0x7f000001/",
        "https://127.1/",  # shortened
        "https://127.0.0.1.5/",
        "https://01.1.1.1/",  # leading zero
        "https://256.1.1.1/",
        "https://[fe80::1%25eth0]/",  # zone id
        "https://[::1/",
        "https://[not-ipv6]/",
        "https://example.com:0/",
        "https://example.com:65536/",
        "https://example.com:08080/",
        "https://example.com:/",
        "https://example.com:http/",
        "https://exa mple.com/",
        "https://-bad-.example.com/",
        "https://example.com/a/../b",
        "https://example.com/a/%2e%2e/b",
        "https://example.com/a%2fb",
        "https://example.com/#fragment",
        "https://example.com/" + "a" * 2100,
        "https://example.123/",  # numeric last label: ambiguous IPv4
    ],
)
def test_ambiguous_or_invalid_urls_are_rejected(url: str) -> None:
    with pytest.raises(EgressDeniedError) as exc_info:
        parse_egress_url(url)
    assert exc_info.value.reason is EgressDenialReason.INVALID_URL


@pytest.mark.parametrize(
    "url", ["ftp://example.com/", "file:///etc/passwd", "gopher://x/", "javascript:alert(1)"]
)
def test_only_http_and_https_are_allowed(url: str) -> None:
    with pytest.raises(EgressDeniedError) as exc_info:
        parse_egress_url(url)
    assert exc_info.value.reason is EgressDenialReason.SCHEME_NOT_ALLOWED


def test_invalid_url_error_does_not_echo_credentials() -> None:
    with pytest.raises(EgressDeniedError) as exc_info:
        parse_egress_url("https://admin:hunter2@example.com/")
    assert "hunter2" not in str(exc_info.value)


# --- address classes ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("8.8.8.8", AddressClass.PUBLIC),
        ("2606:4700:4700::1111", AddressClass.PUBLIC),
        ("127.0.0.1", AddressClass.LOOPBACK),
        ("127.255.0.1", AddressClass.LOOPBACK),
        ("::1", AddressClass.LOOPBACK),
        ("10.1.2.3", AddressClass.PRIVATE),
        ("172.16.0.1", AddressClass.PRIVATE),
        ("172.31.255.255", AddressClass.PRIVATE),
        ("192.168.1.1", AddressClass.PRIVATE),
        ("fd12:3456::1", AddressClass.PRIVATE),
        ("100.64.0.1", AddressClass.SPECIAL),  # CGNAT
        ("198.18.0.1", AddressClass.SPECIAL),  # benchmarking
        ("192.0.2.1", AddressClass.SPECIAL),  # documentation
        ("203.0.113.9", AddressClass.SPECIAL),
        ("240.0.0.1", AddressClass.SPECIAL),  # reserved
        ("192.0.0.8", AddressClass.SPECIAL),
        ("2001:db8::1", AddressClass.SPECIAL),
        ("100::1", AddressClass.SPECIAL),
        ("169.254.169.254", AddressClass.NEVER),  # cloud metadata
        ("169.254.1.1", AddressClass.NEVER),
        ("100.100.100.200", AddressClass.NEVER),
        ("fd00:ec2::254", AddressClass.NEVER),
        ("fe80::1", AddressClass.NEVER),
        ("0.0.0.0", AddressClass.NEVER),  # noqa: S104 - an address under test, not a bind
        ("0.1.2.3", AddressClass.NEVER),
        ("::", AddressClass.NEVER),
        ("224.0.0.1", AddressClass.NEVER),
        ("239.255.255.250", AddressClass.NEVER),
        ("255.255.255.255", AddressClass.NEVER),
        ("ff02::1", AddressClass.NEVER),
    ],
)
def test_address_classes(address: str, expected: AddressClass) -> None:
    assert classify(ip_address(address)) is expected


@pytest.mark.parametrize(
    ("address", "embedded"),
    [
        ("::ffff:127.0.0.1", "127.0.0.1"),  # IPv4-mapped
        ("::ffff:169.254.169.254", "169.254.169.254"),
        ("64:ff9b::7f00:1", "127.0.0.1"),  # NAT64 well-known prefix
        ("64:ff9b:1::a9fe:a9fe", "169.254.169.254"),  # NAT64 local-use
        ("2002:7f00:1::1", "127.0.0.1"),  # 6to4
        ("2001:0:4136:e378:8000:63bf:80ff:fffe", "127.0.0.1"),  # Teredo (client address is obfuscated)
        ("::127.0.0.1", "127.0.0.1"),  # deprecated IPv4-compatible
        ("2606:4700:4700::1111", None),
        ("::1", None),
    ],
)
def test_embedded_ipv4_is_unwrapped(address: str, embedded: str | None) -> None:
    result = embedded_ipv4(ip_address(address))
    assert result == (IPv4Address(embedded) if embedded else None)


@pytest.mark.parametrize(
    "address",
    [
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::1",
        "2001:0:4136:e378:8000:63bf:80ff:fffe",
        "::ffff:8.8.8.8",
    ],
)
def test_wrapped_addresses_are_never_public(address: str) -> None:
    assert not is_public(ip_address(address))


def test_wrapped_metadata_is_never_allowed() -> None:
    assert is_never_allowed(ip_address("::ffff:169.254.169.254"))
    assert is_never_allowed(ip_address("64:ff9b::a9fe:a9fe"))
    assert not is_never_allowed(ip_address("64:ff9b::808:808"))


# --- policy ----------------------------------------------------------------------------------------------


def _policy(config: bytes = OLLAMA_CONFIG, forbidden: tuple[str, ...] = ()) -> EgressPolicy:
    return build_egress_policy(
        parse_instance_config(config), [("PostgreSQL", ip_address(address)) for address in forbidden]
    )


def test_public_https_is_allowed_without_an_entry() -> None:
    policy = _policy(b"")
    rule = policy.check_url("https://api.openai.com/v1", LLM, "openai")
    assert rule == PUBLIC_RULE
    policy.check_addresses(rule, [ip_address("104.18.6.192"), ip_address("2606:4700::6812:6c0")])


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "172.30.10.7", "100.64.1.1", "::1", "fd00::1"])
def test_internal_addresses_are_denied_without_an_entry(address: str) -> None:
    with pytest.raises(EgressDeniedError) as exc_info:
        _policy().check_addresses(PUBLIC_RULE, [ip_address(address)])
    assert exc_info.value.reason is EgressDenialReason.ADDRESS_NOT_ALLOWED


def test_plain_http_needs_an_entry() -> None:
    with pytest.raises(EgressDeniedError) as exc_info:
        _policy().check_url("http://api.openai.com/v1", LLM, "openai")
    assert exc_info.value.reason is EgressDenialReason.SCHEME_NOT_ALLOWED


def test_ip_literal_url_is_checked_without_dns() -> None:
    for url in (
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://169.254.169.254/latest",
        "https://[::ffff:a9fe:a9fe]/",
    ):
        with pytest.raises(EgressDeniedError):
            _policy().check_url(url, LLM, None)


def test_entry_allows_exact_origin_purpose_and_provider_type() -> None:
    policy = _policy()
    rule = policy.check_url("http://ollama:11434/api/chat", LLM, "ollama")
    assert rule == EgressRule(entry="local-ollama", allowed_cidrs=(ip_network("172.30.10.0/24"),))
    policy.check_addresses(rule, [ip_address("172.30.10.7")])
    assert policy.check_url("http://ollama:11434", EMBEDDING, "ollama").entry == "local-ollama"


@pytest.mark.parametrize(
    ("url", "purpose", "provider_type"),
    [
        ("http://ollama:11434/", LLM, "openai_compatible"),  # other provider type (SC-06)
        ("http://ollama:11435/", LLM, "ollama"),  # other port
        ("http://ollama.evil.com:11434/", LLM, "ollama"),
        ("http://ollama:11434/", LLM, None),
    ],
)
def test_entry_does_not_match_other_origins_or_provider_types(
    url: str, purpose: EgressPurpose, provider_type: str | None
) -> None:
    policy = _policy()
    with pytest.raises(EgressDeniedError):
        policy.check_url(url, purpose, provider_type)


def test_entry_cidrs_bound_the_resolved_addresses() -> None:
    policy = _policy()
    rule = policy.check_url("http://ollama:11434/", LLM, "ollama")
    for address in ("172.30.11.1", "10.0.0.1", "127.0.0.1", "8.8.8.8"):
        with pytest.raises(EgressDeniedError) as exc_info:
            policy.check_addresses(rule, [ip_address(address)])
        assert exc_info.value.reason is EgressDenialReason.ADDRESS_NOT_ALLOWED
    # Every address must pass: one allowed and one other address is refused (DNS answers can be mixed).
    with pytest.raises(EgressDeniedError):
        policy.check_addresses(rule, [ip_address("172.30.10.7"), ip_address("172.30.11.7")])
    # A wrapped form of an allowed address is still outside an IPv4-only rule.
    with pytest.raises(EgressDeniedError):
        policy.check_addresses(rule, [ip_address("::ffff:172.30.10.7")])


def test_metadata_and_platform_addresses_are_never_allowed() -> None:
    config = b"""
[[network.allowlist]]
name = "wide"
url = "http://models.internal:8080"
purposes = ["llm"]
allowed_cidrs = ["10.0.0.0/8"]
"""
    policy = _policy(config, forbidden=("192.168.5.5",))
    rule = policy.check_url("http://models.internal:8080/", LLM, "openai_compatible")
    policy.check_addresses(rule, [ip_address("10.0.0.5")])
    for address in ("169.254.169.254", "192.168.5.5", "::ffff:192.168.5.5"):
        with pytest.raises(EgressDeniedError) as exc_info:
            policy.check_addresses(rule, [ip_address(address)])
        assert exc_info.value.reason is EgressDenialReason.ADDRESS_NEVER_ALLOWED


def test_path_prefix_entry_matches_only_below_the_prefix() -> None:
    policy = _policy()
    assert policy.check_url("https://llm.example.com/v1/chat", LLM, "openai").entry == "gateway"
    assert policy.check_url("https://llm.example.com/v1", LLM, "openai").entry == "gateway"
    # Outside the prefix: no entry, but still a public https URL.
    assert policy.check_url("https://llm.example.com/v10", LLM, "openai").entry is None
    assert policy.check_url("https://llm.example.com/v1/x", EMBEDDING, "openai").entry is None


# --- CONFIG_FILE --------------------------------------------------------------------------------------


def test_config_file_is_optional_and_empty_by_default(tmp_path: Path) -> None:
    assert load_instance_config(None).allowlist == ()
    path = tmp_path / "plummy.toml"
    path.write_text("")
    assert load_instance_config(path).allowlist == ()


def test_config_file_is_parsed(tmp_path: Path) -> None:
    path = tmp_path / "plummy.toml"
    path.write_bytes(OLLAMA_CONFIG)
    config = load_instance_config(path)
    assert [entry.name for entry in config.allowlist] == ["local-ollama", "gateway"]
    ollama = config.allowlist[0]
    assert ollama.provider_types == frozenset({"ollama"})
    assert ollama.purposes == frozenset({LLM, EMBEDDING})
    assert config.allowlist[1].provider_types is None
    assert config.allowlist[1].path_prefix == "/v1/"


def _entry(**fields: object) -> bytes:
    values: dict[str, object] = {
        "name": "x",
        "url": "http://svc:80",
        "purposes": ["llm"],
        "allowed_cidrs": ["10.1.0.0/16"],
    }
    values.update(fields)
    lines = ["[[network.allowlist]]"]
    for key, value in values.items():
        if value is not None:
            rendered = str(value).replace("'", '"') if isinstance(value, list) else f'"{value}"'
            lines.append(f"{key} = {rendered}")
    return "\n".join(lines).encode()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"not = [toml", "not valid TOML"),
        (b"[database]\nurl = 'x'", "unknown sections"),
        (b"[network]\nproxy = 'x'", "may contain only 'allowlist'"),
        (_entry(name="Bad Name"), "name must be"),
        (_entry(url=None), "url is required"),
        (_entry(url="ftp://svc/"), "url:"),
        (_entry(url="http://user:pw@svc/"), "url:"),
        (_entry(url="http://svc:80/?a=1"), "must not have a query"),
        (_entry(purposes=[]), "must not be empty"),
        (_entry(purposes=["webhook"]), "purposes must be from"),
        (_entry(purposes=["llm", "llm"]), "duplicates"),
        (_entry(provider_types=["gemini"]), "provider_types must be"),
        (_entry(provider_types=[]), "provider_types must be"),
        (_entry(allowed_cidrs=["10.1.0.1/16"]), "host bits"),
        (_entry(allowed_cidrs=["nonsense"]), "invalid CIDR"),
        (_entry(allowed_cidrs=["0.0.0.0/0"]), "overlaps"),
        (_entry(allowed_cidrs=["169.254.0.0/16"]), "overlaps"),
        (_entry(allowed_cidrs=["::/0"]), "overlaps"),
        (
            _entry(url="http://10.2.0.1:80", allowed_cidrs=["10.1.0.0/16"]),
            "must contain the internal address",
        ),
        (_entry(extra="1"), "unknown keys"),
        (_entry() + b"\n" + _entry(), "names must be unique"),
    ],
)
def test_invalid_config_is_rejected(data: bytes, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message) as exc_info:
        parse_instance_config(data)
    assert "CONFIG_FILE" in str(exc_info.value)
    assert "pw@" not in str(exc_info.value)


def test_unreadable_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="cannot read"):
        load_instance_config(tmp_path / "missing.toml")


def test_allowlist_overlapping_platform_services_is_a_configuration_error() -> None:
    config = parse_instance_config(_entry(allowed_cidrs=["10.1.0.0/16"]))
    with pytest.raises(ConfigurationError, match="includes the address of Redis"):
        build_egress_policy(config, [("Redis", ip_address("10.1.2.3"))])
    policy = build_egress_policy(config, [("Redis", ip_address("10.2.2.3"))])
    assert ip_address("10.2.2.3") in policy.forbidden_addresses
