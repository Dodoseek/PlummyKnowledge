"""A tiny real HTTP/1.1 server on loopback for SafeHttpClient and provider contract tests.

Real sockets (and optionally real TLS) instead of mocked transports: pinning, SNI, certificate checks and
size limits are exercised end to end.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass
class ReceivedRequest:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class Reply:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    chunks: list[bytes] | None = None  # chunked transfer encoding
    delay: float = 0.0

    @classmethod
    def json(cls, payload: Any, status: int = 200, **headers: str) -> Reply:
        return cls(status, {"Content-Type": "application/json", **headers}, json.dumps(payload).encode())


Handler = Callable[[ReceivedRequest], Reply | Awaitable[Reply]]


@dataclass
class FakeHttpServer:
    port: int
    requests: list[ReceivedRequest]
    connections: int = 0


async def _read_request(reader: asyncio.StreamReader) -> ReceivedRequest | None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    lines = head.decode("latin-1").split("\r\n")
    method, target, _ = lines[0].split(" ", 2)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if line:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
    body = b""
    if "content-length" in headers:
        body = await reader.readexactly(int(headers["content-length"]))
    return ReceivedRequest(method, target, headers, body)


def _encode(reply: Reply) -> list[bytes]:
    head = [f"HTTP/1.1 {reply.status} X"]
    headers = {"Connection": "close", **reply.headers}
    if reply.chunks is None:
        headers.setdefault("Content-Length", str(len(reply.body)))
    else:
        headers["Transfer-Encoding"] = "chunked"
    head += [f"{name}: {value}" for name, value in headers.items()]
    parts = [("\r\n".join(head) + "\r\n\r\n").encode("latin-1")]
    if reply.chunks is None:
        parts.append(reply.body)
    else:
        parts += [f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n" for chunk in reply.chunks]
        parts.append(b"0\r\n\r\n")
    return parts


@asynccontextmanager
async def serve(
    handler: Handler, *, ssl_context: ssl.SSLContext | None = None
) -> AsyncIterator[FakeHttpServer]:
    state = FakeHttpServer(port=0, requests=[])

    async def on_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        state.connections += 1
        try:
            request = await _read_request(reader)
            if request is None:
                return
            state.requests.append(request)
            reply = handler(request)
            if not isinstance(reply, Reply):
                reply = await reply
            parts = _encode(reply)
            writer.write(parts[0])
            for part in parts[1:]:
                if reply.delay:
                    await writer.drain()
                    await asyncio.sleep(reply.delay)
                writer.write(part)
            await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(on_connection, "127.0.0.1", 0, ssl=ssl_context)
    state.port = server.sockets[0].getsockname()[1]
    try:
        yield state
    finally:
        server.close()
        await server.wait_closed()


@dataclass(frozen=True)
class LocalPki:
    ca_file: Path
    server_context: ssl.SSLContext

    def client_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.ca_file))


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def make_pki(directory: Path, server_name: str, *other_names: str) -> LocalPki:
    """A throw-away CA and a server certificate for ``server_name`` (and ``other_names``, nothing else)."""
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("plummy test CA"))
        .issuer_name(_name("plummy test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(server_name))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in (server_name, *other_names)]),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_file, cert_file, key_file = directory / "ca.pem", directory / "server.pem", directory / "server.key"
    ca_file.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_context.load_cert_chain(cert_file, key_file)
    return LocalPki(ca_file=ca_file, server_context=server_context)


LOOPBACK = IPv4Address("127.0.0.1")
