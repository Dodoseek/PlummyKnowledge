"""Redirects followed for connectors (ADR-007 §4 step 7, ADR-020 §5): each hop is checked and pinned again.

One TLS server on loopback answers for two names (the API host and the download host); both are reachable only
through allowlist entries with ``127.0.0.1/32``, so every hop must pass the policy on its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from plummy.core.instance_config import build_egress_policy, parse_instance_config
from plummy.core.network.errors import EgressDenialReason, EgressDeniedError, ResponseTooLargeError
from plummy.core.network.policy import EgressPolicy, EgressPurpose, RedirectPolicy
from tests.fake_http import ReceivedRequest, Reply, make_pki, serve
from tests.unit.test_safe_http_client import FakeResolver, Harness

CONNECTOR = EgressPurpose.CONNECTOR
API = "api.drive.test"
DOWNLOAD = "dl.content.test"
FOLLOW = RedirectPolicy(hosts=frozenset({API, "*.content.test"}))
TOKEN = "Bearer test-access-token"


def _policy(port: int, *hosts: str) -> EgressPolicy:
    entries = "".join(
        f"""
[[network.allowlist]]
name = "fake-{index}"
url = "https://{host}:{port}"
purposes = ["connector"]
provider_types = ["google_drive"]
allowed_cidrs = ["127.0.0.1/32"]
"""
        for index, host in enumerate(hosts)
    )
    return build_egress_policy(parse_instance_config(entries.encode()))


def _routes(port: int, **extra: Reply) -> Any:
    def handler(request: ReceivedRequest) -> Reply:
        path = request.target.split("?", 1)[0]
        if path in extra:
            return extra[path]
        if path == "/file":
            return Reply(302, {"Location": f"https://{DOWNLOAD}:{port}/blob"})
        if path == "/relative":
            return Reply(307, {"Location": "/blob"})
        if path == "/blob":
            return Reply(200, {"Content-Type": "application/octet-stream"}, b"file-bytes")
        return Reply(404)

    return handler


class _Server:
    """A TLS server for both names whose routes know its own port."""

    def __init__(self, tmp_path: Path) -> None:
        self.pki = make_pki(tmp_path, API, DOWNLOAD)
        self.port = 0
        self.extra: dict[str, Reply] = {}

    def handler(self, request: ReceivedRequest) -> Reply:
        return _routes(self.port, **self.extra)(request)


async def _download(
    tmp_path: Path,
    path: str,
    *,
    resolver: dict[str, list[list[str]]] | None = None,
    extra: dict[str, Reply] | None = None,
    hosts: tuple[str, ...] = (API, DOWNLOAD),
    redirects: RedirectPolicy | None = FOLLOW,
    max_response_bytes: int = 1024,
) -> tuple[Harness, list[ReceivedRequest], bytes | Exception, int | None]:
    server_state = _Server(tmp_path)
    server_state.extra = extra or {}
    async with serve(server_state.handler, ssl_context=server_state.pki.server_context) as server:
        server_state.port = server.port
        harness = Harness(
            _policy(server.port, *hosts),
            FakeResolver(resolver or {API: [["127.0.0.1"]], DOWNLOAD: [["127.0.0.1"]]}),
            ssl_context=server_state.pki.client_context(),
        )
        outcome: bytes | Exception
        status: int | None = None
        try:
            async with harness.client.stream(
                "GET",
                f"https://{API}:{server.port}{path}",
                purpose=CONNECTOR,
                provider_type="google_drive",
                headers={"Authorization": TOKEN},
                redirects=redirects,
                max_response_bytes=max_response_bytes,
            ) as response:
                status = response.status_code
                outcome = await response.aread()
        except (EgressDeniedError, ResponseTooLargeError) as exc:
            outcome = exc
        finally:
            await harness.client.aclose()
        return harness, list(server.requests), outcome, status


async def test_redirect_to_a_listed_host_is_followed_without_credentials(tmp_path: Path) -> None:
    harness, requests, body, status = await _download(tmp_path, "/file")
    assert status == 200
    assert body == b"file-bytes"
    assert [request.headers["host"].split(":")[0] for request in requests] == [API, DOWNLOAD]
    assert requests[0].headers["authorization"] == TOKEN
    assert "authorization" not in requests[1].headers  # not sent to another host
    assert harness.resolver.lookups == [API, DOWNLOAD]  # every hop resolved and pinned on its own
    assert harness.backend.connected_to == ["127.0.0.1", "127.0.0.1"]


async def test_relative_redirect_on_the_same_host_keeps_credentials(tmp_path: Path) -> None:
    _, requests, body, status = await _download(tmp_path, "/relative")
    assert (status, body) == (200, b"file-bytes")
    assert [request.headers.get("authorization") for request in requests] == [TOKEN, TOKEN]


async def test_without_a_redirect_policy_the_redirect_is_returned(tmp_path: Path) -> None:
    _, requests, _, status = await _download(tmp_path, "/file", redirects=None)
    assert status == 302
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("https://evil.example:{port}/blob", EgressDenialReason.REDIRECT_NOT_ALLOWED),
        ("https://content.test:{port}/blob", EgressDenialReason.REDIRECT_NOT_ALLOWED),  # apex is not *.
        ("http://dl.content.test:{port}/blob", EgressDenialReason.REDIRECT_NOT_ALLOWED),  # not https
        ("https://169.254.169.254/latest/meta-data/", EgressDenialReason.REDIRECT_NOT_ALLOWED),
        ("https://user@dl.content.test:{port}/blob", EgressDenialReason.INVALID_URL),
    ],
)
async def test_redirect_outside_the_connector_hosts_is_denied_and_reported(
    tmp_path: Path, location: str, reason: EgressDenialReason
) -> None:
    server_state = _Server(tmp_path)
    async with serve(server_state.handler, ssl_context=server_state.pki.server_context) as server:
        server_state.port = server.port
        server_state.extra = {"/bad": Reply(302, {"Location": location.format(port=server.port)})}
        harness = Harness(
            _policy(server.port, API, DOWNLOAD),
            FakeResolver({API: [["127.0.0.1"]], DOWNLOAD: [["127.0.0.1"]]}),
            ssl_context=server_state.pki.client_context(),
        )
        with pytest.raises(EgressDeniedError) as exc_info:
            await harness.client.request(
                "GET",
                f"https://{API}:{server.port}/bad",
                purpose=CONNECTOR,
                provider_type="google_drive",
                redirects=FOLLOW,
            )
        await harness.client.aclose()
    assert exc_info.value.reason is reason
    assert len(server.requests) == 1  # the target is never contacted
    assert [denial.reason for denial in harness.denials] == [reason]
    assert harness.denials[0].purpose is CONNECTOR


async def test_listed_host_resolving_to_metadata_is_denied_on_the_hop(tmp_path: Path) -> None:
    """The host list does not replace the address check: the hop is resolved and checked again."""
    harness, requests, outcome, _ = await _download(
        tmp_path, "/file", resolver={API: [["127.0.0.1"]], DOWNLOAD: [["169.254.169.254"]]}
    )
    assert isinstance(outcome, EgressDeniedError)
    assert outcome.reason is EgressDenialReason.ADDRESS_NEVER_ALLOWED
    assert len(requests) == 1
    assert harness.backend.connected_to == ["127.0.0.1"]


async def test_listed_host_without_an_allowlist_entry_is_denied_for_an_internal_address(
    tmp_path: Path,
) -> None:
    _, requests, outcome, _ = await _download(tmp_path, "/file", hosts=(API,))
    assert isinstance(outcome, EgressDeniedError)
    assert outcome.reason is EgressDenialReason.ADDRESS_NOT_ALLOWED
    assert len(requests) == 1


async def test_more_than_three_hops_are_refused(tmp_path: Path) -> None:
    loop = {f"/hop{index}": Reply(302, {"Location": f"/hop{index + 1}"}) for index in range(5)}
    harness, requests, outcome, _ = await _download(tmp_path, "/hop0", extra=loop)
    assert isinstance(outcome, EgressDeniedError)
    assert outcome.reason is EgressDenialReason.TOO_MANY_REDIRECTS
    assert len(requests) == 4  # the original request and three followed hops
    assert harness.denials[0].reason is EgressDenialReason.TOO_MANY_REDIRECTS


async def test_size_limit_applies_to_the_final_response(tmp_path: Path) -> None:
    big = {"/blob": Reply(200, {}, b"x" * 2048)}
    _, _, outcome, _ = await _download(tmp_path, "/file", extra=big, max_response_bytes=1024)
    assert isinstance(outcome, ResponseTooLargeError)


async def test_request_with_redirects_reads_the_final_body(tmp_path: Path) -> None:
    server_state = _Server(tmp_path)
    async with serve(server_state.handler, ssl_context=server_state.pki.server_context) as server:
        server_state.port = server.port
        harness = Harness(
            _policy(server.port, API, DOWNLOAD),
            FakeResolver({API: [["127.0.0.1"]], DOWNLOAD: [["127.0.0.1"]]}),
            ssl_context=server_state.pki.client_context(),
        )
        response = await harness.client.request(
            "GET",
            f"https://{API}:{server.port}/file",
            purpose=CONNECTOR,
            provider_type="google_drive",
            redirects=FOLLOW,
        )
        await harness.client.aclose()
    assert response.status_code == 200
    assert response.content == b"file-bytes"


async def test_redirects_are_not_followed_for_requests_with_a_body() -> None:
    harness = Harness(EgressPolicy(), FakeResolver({}))
    with pytest.raises(ValueError, match="GET and HEAD"):
        await harness.client.request(
            "POST", f"https://{API}/token", purpose=CONNECTOR, content=b"x", redirects=FOLLOW
        )
    await harness.client.aclose()
    assert harness.resolver.lookups == []


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        (API, True),
        ("dl.content.test", True),
        ("a.b.content.test", True),
        ("content.test", False),
        ("evilcontent.test", False),
        ("content.test.evil", False),
        ("API.drive.test", False),  # hosts arrive normalized; the pattern is exact
        ("other.drive.test", False),
    ],
)
def test_redirect_host_patterns(host: str, allowed: bool) -> None:
    assert FOLLOW.allows_host(host) is allowed


@pytest.mark.parametrize("pattern", ["", "*.", "*.*.test", "Upper.test", "localhost", "*"])
def test_invalid_redirect_host_patterns_are_rejected(pattern: str) -> None:
    with pytest.raises(ValueError, match="invalid redirect host"):
        RedirectPolicy(hosts=frozenset({pattern}))


@pytest.mark.parametrize("hops", [0, 4])
def test_hop_limit_is_bounded(hops: int) -> None:
    with pytest.raises(ValueError, match="max_hops"):
        RedirectPolicy(hosts=frozenset({API}), max_hops=hops)


def test_connector_entries_accept_source_types_but_not_upload() -> None:
    config = parse_instance_config(
        b"""
[[network.allowlist]]
name = "fake-google"
url = "http://fake-google:8080"
purposes = ["connector"]
provider_types = ["google_drive"]
allowed_cidrs = ["10.213.17.0/24"]
"""
    )
    assert config.allowlist[0].purposes == frozenset({CONNECTOR})
    with pytest.raises(Exception, match="provider_types must be"):
        parse_instance_config(
            b"""
[[network.allowlist]]
name = "x"
url = "https://example.com"
purposes = ["connector"]
provider_types = ["upload"]
"""
        )
