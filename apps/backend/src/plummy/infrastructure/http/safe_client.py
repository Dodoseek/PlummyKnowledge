"""``SafeHttpClient``: the only way the application opens outbound HTTP connections (ADR-007).

Enforcement happens where the TCP connection is made, inside a custom ``httpcore`` network backend:

1. the URL is parsed strictly and matched against the allowlist (rule = allowlist entry or "public only");
2. the hostname is resolved **once**; every address is checked against the rule;
3. the socket is opened to the vetted IP itself, so there is no second resolution (no DNS rebinding window);
   the TLS handshake still uses the original hostname for SNI and certificate verification.

Redirects are not followed unless the caller passes a :class:`RedirectPolicy` (connectors only, ADR-007 §4
step 7): then every hop is checked again — ``https``, a host from the connector's list, at most three hops —
and goes through the same pinned transport. Credentials are not sent to a host other than the original one.
Proxy environment variables are ignored, response bodies are size-limited, and every refusal is reported
(``network.egress.denied``) without the path, query or body of the request.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpcore
import httpx

from plummy.core.network.addresses import IPAddress
from plummy.core.network.errors import EgressDenialReason, EgressDeniedError, ResponseTooLargeError
from plummy.core.network.policy import EgressPolicy, EgressPurpose, EgressRule, RedirectPolicy
from plummy.core.network.urls import EgressUrl, parse_egress_url, parse_ip

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESPONSE_BYTES: Final = 10 * 1024 * 1024
DEFAULT_TIMEOUT: Final = httpx.Timeout(30.0, connect=10.0)
_CONTEXT_EXTENSION: Final = "plummy.egress"
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
_CREDENTIAL_HEADERS: Final = frozenset({"authorization", "cookie", "proxy-authorization"})


class Resolver(Protocol):
    async def resolve(self, host: str, port: int) -> list[IPAddress]: ...


class SystemResolver:
    """``getaddrinfo`` through the event loop; the result is used exactly once, for one connection."""

    async def resolve(self, host: str, port: int) -> list[IPAddress]:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
        addresses: list[IPAddress] = []
        for _family, _type, _proto, _canonname, sockaddr in infos:
            address = parse_ip(str(sockaddr[0]).split("%", 1)[0])
            if address is not None and address not in addresses:
                addresses.append(address)
        return addresses


@dataclass(frozen=True, slots=True)
class EgressContext:
    purpose: EgressPurpose
    provider_type: str | None
    max_response_bytes: int


@dataclass(frozen=True, slots=True)
class EgressDenial:
    """What is reported about a refusal: never the path, query, headers or body."""

    purpose: EgressPurpose
    provider_type: str | None
    scheme: str
    host: str
    port: int
    reason: EgressDenialReason
    rule: str | None


DenialHook = Callable[[EgressDenial], Awaitable[None]]


async def _report(denial: EgressDenial, on_denied: DenialHook | None) -> None:
    logger.warning(
        "outbound connection denied",
        extra={
            "purpose": denial.purpose.value,
            "provider_type": denial.provider_type,
            "host": denial.host,
            "port": denial.port,
            "reason": denial.reason.value,
            "rule": denial.rule,
        },
    )
    if on_denied is not None:
        try:
            await on_denied(denial)
        except Exception:
            logger.exception("failed to record an egress denial")


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolves once, checks every address against the rule and connects to the vetted IP."""

    def __init__(
        self,
        policy: EgressPolicy,
        rule: EgressRule,
        resolver: Resolver,
        inner: httpcore.AsyncNetworkBackend,
    ) -> None:
        self._policy = policy
        self._rule = rule
        self._resolver = resolver
        self._inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's backend interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        literal = parse_ip(host.strip("[]"))
        if literal is not None:
            addresses = [literal]
        else:
            try:
                async with asyncio.timeout(timeout):
                    addresses = await self._resolver.resolve(host, port)
            except TimeoutError:
                raise httpcore.ConnectTimeout("name resolution timed out") from None
            except OSError as exc:
                raise httpcore.ConnectError(f"name resolution failed ({type(exc).__name__})") from None
            if not addresses:
                raise httpcore.ConnectError("name resolution returned no addresses")
        self._policy.check_addresses(self._rule, addresses)

        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None  # noqa: S101 - addresses is not empty
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's backend interface
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise EgressDeniedError(EgressDenialReason.UNSUPPORTED_TRANSPORT)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


_EXCEPTIONS: Final[tuple[tuple[type[Exception], type[httpx.TransportError]], ...]] = (
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
)


def _map_exception(exc: Exception, request: httpx.Request) -> Exception:
    for source, target in _EXCEPTIONS:
        if isinstance(exc, source):
            return target(str(exc), request=request)
    if isinstance(exc, httpcore.TimeoutException):
        return httpx.TimeoutException(str(exc), request=request)
    if isinstance(exc, (httpcore.NetworkError, httpcore.ProtocolError)):
        return httpx.TransportError(str(exc), request=request)
    return exc


class _LimitedStream(httpx.AsyncByteStream):
    def __init__(self, stream: Any, request: httpx.Request, limit: int) -> None:
        self._stream = stream
        self._request = request
        self._limit = limit

    async def __aiter__(self) -> AsyncIterator[bytes]:
        received = 0
        try:
            async for chunk in self._stream:
                received += len(chunk)
                if received > self._limit:
                    raise ResponseTooLargeError(self._limit)
                yield chunk
        except httpcore.TimeoutException as exc:
            raise _map_exception(exc, self._request) from exc
        except (httpcore.NetworkError, httpcore.ProtocolError) as exc:
            raise _map_exception(exc, self._request) from exc

    async def aclose(self) -> None:
        if hasattr(self._stream, "aclose"):
            await self._stream.aclose()


class _EgressTransport(httpx.AsyncBaseTransport):
    """One connection pool per rule, so a connection vetted for one rule is never reused under another."""

    def __init__(
        self,
        policy: EgressPolicy,
        resolver: Resolver,
        ssl_context: ssl.SSLContext,
        inner_backend: httpcore.AsyncNetworkBackend,
        limits: httpx.Limits,
        on_denied: DenialHook | None,
    ) -> None:
        self._policy = policy
        self._resolver = resolver
        self._ssl_context = ssl_context
        self._inner_backend = inner_backend
        self._limits = limits
        self._on_denied = on_denied
        self._pools: dict[EgressRule, httpcore.AsyncConnectionPool] = {}

    def _pool(self, rule: EgressRule) -> httpcore.AsyncConnectionPool:
        pool = self._pools.get(rule)
        if pool is None:
            pool = httpcore.AsyncConnectionPool(
                ssl_context=self._ssl_context,
                max_connections=self._limits.max_connections,
                max_keepalive_connections=self._limits.max_keepalive_connections,
                keepalive_expiry=self._limits.keepalive_expiry,
                network_backend=_PinnedNetworkBackend(
                    self._policy, rule, self._resolver, self._inner_backend
                ),
            )
            self._pools[rule] = pool
        return pool

    async def _denied(
        self,
        exc: EgressDeniedError,
        url: EgressUrl | None,
        request: httpx.Request,
        context: EgressContext,
        rule: EgressRule | None,
    ) -> None:
        denial = EgressDenial(
            purpose=context.purpose,
            provider_type=context.provider_type,
            scheme=url.scheme if url else request.url.scheme,
            host=url.host if url else "",
            port=url.port if url else 0,
            reason=exc.reason,
            rule=rule.entry if rule else None,
        )
        await _report(denial, self._on_denied)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        context = request.extensions.get(_CONTEXT_EXTENSION)
        if not isinstance(context, EgressContext):
            raise RuntimeError("requests must be sent through SafeHttpClient")
        url: EgressUrl | None = None
        rule: EgressRule | None = None
        try:
            url = parse_egress_url(str(request.url))
            rule = self._policy.rule_for(url, context.purpose, context.provider_type)
            core_request = httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                # Only the timeout: no caller-controlled SNI or other transport extensions.
                extensions={"timeout": request.extensions.get("timeout", {})},
            )
            response = await self._pool(rule).handle_async_request(core_request)
        except EgressDeniedError as exc:
            await self._denied(exc, url, request, context, rule)
            raise
        except (
            httpcore.TimeoutException,
            httpcore.NetworkError,
            httpcore.ProtocolError,
            httpcore.UnsupportedProtocol,
        ) as exc:
            raise _map_exception(exc, request) from exc

        length = {name.lower(): value for name, value in response.headers}.get(b"content-length")
        if length is not None and length.isdigit() and int(length) > context.max_response_bytes:
            await response.aclose()
            raise ResponseTooLargeError(context.max_response_bytes)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_LimitedStream(response.stream, request, context.max_response_bytes),
            extensions={key: value for key, value in response.extensions.items() if key == "http_version"},
        )

    async def aclose(self) -> None:
        pools, self._pools = list(self._pools.values()), {}
        for pool in pools:
            await pool.aclose()


class SafeHttpClient:
    """Outbound HTTP for adapters (AI providers and document-source connectors). Shared per process."""

    def __init__(
        self,
        policy: EgressPolicy,
        *,
        resolver: Resolver | None = None,
        ssl_context: ssl.SSLContext | None = None,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
        limits: httpx.Limits | None = None,
        on_denied: DenialHook | None = None,
    ) -> None:
        self.policy = policy
        self._on_denied = on_denied
        self._client = httpx.AsyncClient(
            transport=_EgressTransport(
                policy,
                resolver or SystemResolver(),
                ssl_context or httpx.create_ssl_context(),
                network_backend or httpcore.AnyIOBackend(),
                limits
                or httpx.Limits(max_connections=50, max_keepalive_connections=10, keepalive_expiry=30.0),
                on_denied,
            ),
            follow_redirects=False,
            trust_env=False,  # HTTP(S)_PROXY, NO_PROXY and .netrc are ignored (ADR-007 §4.8)
            timeout=DEFAULT_TIMEOUT,
        )

    def _build(
        self,
        method: str,
        url: str,
        *,
        purpose: EgressPurpose,
        provider_type: str | None,
        headers: Mapping[str, str] | None,
        json: Any,
        content: bytes | None,
        params: Mapping[str, str] | None,
        timeout: httpx.Timeout | None,
        max_response_bytes: int,
    ) -> httpx.Request:
        request = self._client.build_request(
            method,
            url,
            headers=headers,
            json=json,
            content=content,
            params=params,
            timeout=timeout or DEFAULT_TIMEOUT,
        )
        request.extensions[_CONTEXT_EXTENSION] = EgressContext(
            purpose=purpose, provider_type=provider_type, max_response_bytes=max_response_bytes
        )
        return request

    async def request(
        self,
        method: str,
        url: str,
        *,
        purpose: EgressPurpose,
        provider_type: str | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        params: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | None = None,  # noqa: ASYNC109 - per-phase httpx timeouts
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        redirects: RedirectPolicy | None = None,
    ) -> httpx.Response:
        """Sends the request and reads the whole (size-limited) body. Redirects are returned, not followed,
        unless ``redirects`` is given (``GET``/``HEAD`` only)."""
        request = self._build(
            method,
            url,
            purpose=purpose,
            provider_type=provider_type,
            headers=headers,
            json=json,
            content=content,
            params=params,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )
        if redirects is None:
            return await self._client.send(request)
        response = await self._send_following(request, redirects)
        try:
            await response.aread()
        finally:
            await response.aclose()
        return response

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        purpose: EgressPurpose,
        provider_type: str | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        params: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | None = None,  # noqa: ASYNC109 - per-phase httpx timeouts
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        redirects: RedirectPolicy | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """A streamed response (SSE, NDJSON, downloads); the size limit applies to the whole stream and to
        each response of a redirect chain."""
        request = self._build(
            method,
            url,
            purpose=purpose,
            provider_type=provider_type,
            headers=headers,
            json=json,
            content=content,
            params=params,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )
        if redirects is None:
            response = await self._client.send(request, stream=True)
        else:
            response = await self._send_following(request, redirects)
        try:
            yield response
        finally:
            await response.aclose()

    async def _send_following(self, request: httpx.Request, redirects: RedirectPolicy) -> httpx.Response:
        """Sends ``request`` and follows redirects under ``redirects``; returns an open streamed response."""
        if request.method not in ("GET", "HEAD"):
            raise ValueError("redirects are followed only for GET and HEAD requests")
        origin_host = request.url.host
        hops = 0
        while True:
            response = await self._client.send(request, stream=True)
            location = response.headers.get("location")
            if response.status_code not in _REDIRECT_STATUSES or not location:
                return response
            await response.aclose()
            hops += 1
            request = await self._next_hop(request, location, redirects, hops, origin_host)

    async def _next_hop(
        self,
        request: httpx.Request,
        location: str,
        redirects: RedirectPolicy,
        hops: int,
        origin_host: str,
    ) -> httpx.Request:
        context = request.extensions[_CONTEXT_EXTENSION]
        assert isinstance(context, EgressContext)  # noqa: S101 - set by _build
        target: EgressUrl | None = None
        try:
            try:
                joined = request.url.join(location)
            except (httpx.InvalidURL, ValueError):
                raise EgressDeniedError(EgressDenialReason.INVALID_URL, "redirect location") from None
            target = parse_egress_url(str(joined))
            if hops > redirects.max_hops:
                raise EgressDeniedError(EgressDenialReason.TOO_MANY_REDIRECTS)
            if target.scheme != "https" or not redirects.allows_host(target.host):
                raise EgressDeniedError(EgressDenialReason.REDIRECT_NOT_ALLOWED)
        except EgressDeniedError as exc:
            await _report(
                EgressDenial(
                    purpose=context.purpose,
                    provider_type=context.provider_type,
                    scheme=target.scheme if target else "",
                    host=target.host if target else "",
                    port=target.port if target else 0,
                    reason=exc.reason,
                    rule=None,
                ),
                self._on_denied,
            )
            raise
        headers = [
            (name, value)
            for name, value in request.headers.multi_items()
            if name.lower() != "host"
            and (joined.host == origin_host or name.lower() not in _CREDENTIAL_HEADERS)
        ]
        return httpx.Request(
            request.method,
            joined,
            headers=headers,
            extensions={"timeout": request.extensions.get("timeout", {}), _CONTEXT_EXTENSION: context},
        )

    async def aclose(self) -> None:
        await self._client.aclose()
