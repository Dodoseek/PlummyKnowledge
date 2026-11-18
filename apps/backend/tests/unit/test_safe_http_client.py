"""SafeHttpClient over real loopback sockets (ADR-007): pinning, SNI/TLS, redirects, proxies, size limits.

Loopback stands in for an internal address: it is reachable only through an allowlist entry with 127.0.0.1/32.
"""

from __future__ import annotations

from collections.abc import Iterable
from ipaddress import ip_address
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest

from plummy.core.instance_config import build_egress_policy, parse_instance_config
from plummy.core.network.addresses import IPAddress
from plummy.core.network.errors import EgressDenialReason, EgressDeniedError, ResponseTooLargeError
from plummy.core.network.policy import EgressPolicy, EgressPurpose
from plummy.infrastructure.http.safe_client import EgressDenial, SafeHttpClient
from tests.fake_http import ReceivedRequest, Reply, make_pki, serve

LLM = EgressPurpose.LLM


class FakeResolver:
    """DNS under test control; counts lookups to prove there is one resolution per connection."""

    def __init__(self, answers: dict[str, list[list[str]]]) -> None:
        self._answers = {host: [list(answer) for answer in answers_] for host, answers_ in answers.items()}
        self.lookups: list[str] = []

    async def resolve(self, host: str, port: int) -> list[IPAddress]:
        self.lookups.append(host)
        answers = self._answers.get(host)
        if not answers:
            raise OSError("NXDOMAIN")
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        return [ip_address(item) for item in answer]


class RecordingBackend(httpcore.AsyncNetworkBackend):
    """The real socket backend, recording which host each TCP connection was opened to."""

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()
        self.connected_to: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connected_to.append(host)
        return await self._inner.connect_tcp(host, port, timeout, local_address, socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _policy(
    port: int,
    *,
    scheme: str = "http",
    host: str = "model.test",
    purposes: str = '["llm"]',
    provider_types: str = '["ollama"]',
) -> EgressPolicy:
    config = f"""
[[network.allowlist]]
name = "local-model"
url = "{scheme}://{host}:{port}"
purposes = {purposes}
provider_types = {provider_types}
allowed_cidrs = ["127.0.0.1/32"]
"""
    return build_egress_policy(parse_instance_config(config.encode()))


class Harness:
    def __init__(self, policy: EgressPolicy, resolver: FakeResolver, **kwargs: Any) -> None:
        self.denials: list[EgressDenial] = []
        self.backend = RecordingBackend()
        self.resolver = resolver

        async def record(denial: EgressDenial) -> None:
            self.denials.append(denial)

        self.client = SafeHttpClient(
            policy, resolver=resolver, network_backend=self.backend, on_denied=record, **kwargs
        )


def ok(_: ReceivedRequest) -> Reply:
    return Reply.json({"ok": True})


# --- allowlisted internal endpoint ----------------------------------------------------------------------


async def test_allowlisted_endpoint_is_reached_via_the_pinned_address() -> None:
    async with serve(ok) as server:
        harness = Harness(_policy(server.port), FakeResolver({"model.test": [["127.0.0.1"]]}))
        try:
            response = await harness.client.request(
                "POST",
                f"http://model.test:{server.port}/api/chat",
                purpose=LLM,
                provider_type="ollama",
                json={"q": 1},
            )
        finally:
            await harness.client.aclose()
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert harness.backend.connected_to == ["127.0.0.1"]  # the socket goes to the vetted IP
    assert harness.resolver.lookups == ["model.test"]  # exactly one resolution
    assert server.requests[0].headers["host"] == f"model.test:{server.port}"  # original Host header
    assert server.requests[0].json() == {"q": 1}


@pytest.mark.parametrize(
    ("purpose", "provider_type"),
    [(EgressPurpose.EMBEDDING, "ollama"), (LLM, "openai_compatible"), (LLM, None)],
)
async def test_same_address_for_another_purpose_or_provider_type_is_denied(
    purpose: EgressPurpose, provider_type: str | None
) -> None:
    """SC-06: the entry covers only its purposes and provider types."""
    async with serve(ok) as server:
        harness = Harness(_policy(server.port), FakeResolver({"model.test": [["127.0.0.1"]]}))
        with pytest.raises(EgressDeniedError) as exc_info:
            await harness.client.request(
                "GET", f"http://model.test:{server.port}/", purpose=purpose, provider_type=provider_type
            )
        await harness.client.aclose()
    assert exc_info.value.reason is EgressDenialReason.SCHEME_NOT_ALLOWED
    assert server.connections == 0
    assert harness.denials[0].reason is EgressDenialReason.SCHEME_NOT_ALLOWED


@pytest.mark.parametrize(
    "answer",
    [
        ["127.0.0.2"],  # outside allowed_cidrs
        ["169.254.169.254"],  # metadata
        ["127.0.0.1", "10.0.0.1"],  # one allowed and one not: all must pass
        ["::ffff:169.254.169.254"],
    ],
)
async def test_allowlisted_name_resolving_elsewhere_is_denied(answer: list[str]) -> None:
    """SC-07: the name is allowed, the address it resolves to is not; nothing is connected."""
    async with serve(ok) as server:
        harness = Harness(_policy(server.port), FakeResolver({"model.test": [answer]}))
        with pytest.raises(EgressDeniedError):
            await harness.client.request(
                "GET", f"http://model.test:{server.port}/", purpose=LLM, provider_type="ollama"
            )
        await harness.client.aclose()
    assert harness.backend.connected_to == []
    assert server.connections == 0
    assert harness.resolver.lookups == ["model.test"]
    denial = harness.denials[0]
    assert (denial.host, denial.port, denial.rule) == ("model.test", server.port, "local-model")


async def test_dns_rebinding_cannot_swap_the_address_after_the_check() -> None:
    """The first answer is checked and connected to; a later answer (rebinding) is never used for it."""
    async with serve(ok) as server:
        resolver = FakeResolver({"model.test": [["127.0.0.1"], ["169.254.169.254"]]})
        harness = Harness(_policy(server.port), resolver)
        url = f"http://model.test:{server.port}/"
        first = await harness.client.request("GET", url, purpose=LLM, provider_type="ollama")
        assert first.status_code == 200
        # The server closes connections, so the next request opens a new one and resolves again.
        with pytest.raises(EgressDeniedError):
            await harness.client.request("GET", url, purpose=LLM, provider_type="ollama")
        await harness.client.aclose()
    assert harness.backend.connected_to == ["127.0.0.1"]
    assert resolver.lookups == ["model.test", "model.test"]


# --- public-only default --------------------------------------------------------------------------------


async def test_without_an_entry_internal_names_and_literals_are_denied() -> None:
    async with serve(ok) as server:
        harness = Harness(EgressPolicy(), FakeResolver({"internal.test": [["127.0.0.1"]]}))
        for url in (f"https://internal.test:{server.port}/", f"https://127.0.0.1:{server.port}/"):
            with pytest.raises(EgressDeniedError) as exc_info:
                await harness.client.request("GET", url, purpose=LLM, provider_type="openai")
            assert exc_info.value.reason is EgressDenialReason.ADDRESS_NOT_ALLOWED
        with pytest.raises(EgressDeniedError) as exc_info:
            await harness.client.request("GET", f"http://internal.test:{server.port}/", purpose=LLM)
        assert exc_info.value.reason is EgressDenialReason.SCHEME_NOT_ALLOWED
        await harness.client.aclose()
    assert server.connections == 0
    assert len(harness.denials) == 3


async def test_denial_report_contains_no_path_query_or_credentials() -> None:
    harness = Harness(EgressPolicy(), FakeResolver({"internal.test": [["10.0.0.1"]]}))
    with pytest.raises(EgressDeniedError):
        await harness.client.request(
            "GET",
            "https://internal.test/v1/secret-path?api_key=sk-123",
            purpose=LLM,
            headers={"Authorization": "Bearer sk-123"},
        )
    await harness.client.aclose()
    rendered = repr(harness.denials)
    assert "secret-path" not in rendered
    assert "sk-123" not in rendered


async def test_unresolvable_name_is_a_connection_error() -> None:
    harness = Harness(EgressPolicy(), FakeResolver({}))
    with pytest.raises(httpx.ConnectError):
        await harness.client.request("GET", "https://missing.test/", purpose=LLM)
    await harness.client.aclose()
    assert harness.denials == []


# --- TLS: SNI and certificate verification use the original hostname -----------------------------------


async def test_https_to_pinned_ip_verifies_the_certificate_of_the_hostname(tmp_path: Path) -> None:
    pki = make_pki(tmp_path, "model.test")
    async with serve(ok, ssl_context=pki.server_context) as server:
        harness = Harness(
            _policy(server.port, scheme="https"),
            FakeResolver({"model.test": [["127.0.0.1"]]}),
            ssl_context=pki.client_context(),
        )
        response = await harness.client.request(
            "GET", f"https://model.test:{server.port}/", purpose=LLM, provider_type="ollama"
        )
        await harness.client.aclose()
    assert response.status_code == 200
    assert harness.backend.connected_to == ["127.0.0.1"]


async def test_certificate_for_another_name_is_rejected(tmp_path: Path) -> None:
    pki = make_pki(tmp_path, "other.test")
    async with serve(ok, ssl_context=pki.server_context) as server:
        harness = Harness(
            _policy(server.port, scheme="https"),
            FakeResolver({"model.test": [["127.0.0.1"]]}),
            ssl_context=pki.client_context(),
        )
        with pytest.raises(httpx.ConnectError):
            await harness.client.request(
                "GET", f"https://model.test:{server.port}/", purpose=LLM, provider_type="ollama"
            )
        await harness.client.aclose()
    assert server.requests == []


# --- redirects, proxies, limits ------------------------------------------------------------------------


async def test_redirects_are_returned_not_followed() -> None:
    def redirect(_: ReceivedRequest) -> Reply:
        return Reply(302, {"Location": "http://169.254.169.254/latest/meta-data/"})

    async with serve(redirect) as server:
        harness = Harness(_policy(server.port), FakeResolver({"model.test": [["127.0.0.1"]]}))
        response = await harness.client.request(
            "GET", f"http://model.test:{server.port}/", purpose=LLM, provider_type="ollama"
        )
        await harness.client.aclose()
    assert response.status_code == 302
    assert len(server.requests) == 1
    assert harness.backend.connected_to == ["127.0.0.1"]


async def test_proxy_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.9:3128")
    async with serve(ok) as server:
        harness = Harness(_policy(server.port), FakeResolver({"model.test": [["127.0.0.1"]]}))
        response = await harness.client.request(
            "GET", f"http://model.test:{server.port}/", purpose=LLM, provider_type="ollama"
        )
        await harness.client.aclose()
    assert response.status_code == 200
    assert harness.backend.connected_to == ["127.0.0.1"]


@pytest.mark.parametrize("chunked", [False, True])
async def test_response_size_is_limited(chunked: bool) -> None:
    def big(_: ReceivedRequest) -> Reply:
        return Reply(200, {}, b"x" * 4096) if not chunked else Reply(200, {}, chunks=[b"x" * 1024] * 4)

    async with serve(big) as server:
        harness = Harness(_policy(server.port), FakeResolver({"model.test": [["127.0.0.1"]]}))
        with pytest.raises(ResponseTooLargeError):
            await harness.client.request(
                "GET",
                f"http://model.test:{server.port}/",
                purpose=LLM,
                provider_type="ollama",
                max_response_bytes=2048,
            )
        response = await harness.client.request(
            "GET",
            f"http://model.test:{server.port}/",
            purpose=LLM,
            provider_type="ollama",
            max_response_bytes=4096,
        )
        await harness.client.aclose()
    assert len(response.content) == 4096


async def test_streamed_responses_are_limited_too() -> None:
    async with serve(lambda _: Reply(200, {}, chunks=[b"data: x\n\n"] * 50)) as server:
        harness = Harness(_policy(server.port), FakeResolver({"model.test": [["127.0.0.1"]]}))
        received: list[bytes] = []

        async def consume() -> None:
            async with harness.client.stream(
                "GET",
                f"http://model.test:{server.port}/",
                purpose=LLM,
                provider_type="ollama",
                max_response_bytes=100,
            ) as response:
                async for chunk in response.aiter_bytes():
                    received.append(chunk)

        with pytest.raises(ResponseTooLargeError):
            await consume()
        await harness.client.aclose()
    assert sum(map(len, received)) <= 100


async def test_read_timeout_is_a_timeout_error() -> None:
    async with serve(lambda _: Reply(200, {}, chunks=[b"a", b"b"], delay=1.0)) as server:
        harness = Harness(_policy(server.port), FakeResolver({"model.test": [["127.0.0.1"]]}))
        with pytest.raises(httpx.ReadTimeout):
            await harness.client.request(
                "GET",
                f"http://model.test:{server.port}/",
                purpose=LLM,
                provider_type="ollama",
                timeout=httpx.Timeout(0.2),
            )
        await harness.client.aclose()


async def test_requests_must_go_through_the_client_api() -> None:
    """The transport refuses requests without an egress context (no purpose = no policy decision)."""
    client = SafeHttpClient(EgressPolicy())
    with pytest.raises(RuntimeError, match="SafeHttpClient"):
        await client._client.get("https://example.com/")
    await client.aclose()
