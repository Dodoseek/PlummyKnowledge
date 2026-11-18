from __future__ import annotations

import asyncio
import time

import pytest

from plummy.application.errors import PermissionDeniedError
from plummy.application.security import SecurityContext
from plummy.core.ids import new_id
from plummy.core.security import (
    constant_time_equals,
    derive_csrf_token,
    generate_token,
    hash_token,
    is_well_formed_token,
)
from plummy.domain.identity import Permission
from plummy.infrastructure.security.passwords import Argon2PasswordHasher


def test_tokens_are_random_256_bit_and_well_formed() -> None:
    tokens = {generate_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(len(token) == 43 and is_well_formed_token(token) for token in tokens)
    assert not is_well_formed_token("x" * 42)
    assert not is_well_formed_token("x" * 42 + "!")


def test_token_hash_is_sha256() -> None:
    assert len(hash_token(generate_token())) == 32


def test_csrf_token_is_bound_to_the_session() -> None:
    first, second = generate_token(), generate_token()
    assert derive_csrf_token(first) == derive_csrf_token(first)
    assert derive_csrf_token(first) != derive_csrf_token(second)
    assert first not in derive_csrf_token(first)
    assert constant_time_equals(derive_csrf_token(first), derive_csrf_token(first))
    assert not constant_time_equals(derive_csrf_token(first), derive_csrf_token(second))


def test_uuid7_is_versioned_and_time_ordered() -> None:
    first = new_id()
    time.sleep(0.002)
    second = new_id()
    assert first.version == 7
    assert second.version == 7
    assert first.variant == "specified in RFC 4122"
    assert first < second


def test_security_context_requires_every_permission() -> None:
    context = SecurityContext(
        user_id=new_id(),
        session_id=new_id(),
        email="a@example.com",
        display_name="A",
        roles=frozenset({"member"}),
        permissions=frozenset({"chat.use"}),
        session_expires_at=None,  # type: ignore[arg-type]
        session_idle_expires_at=None,  # type: ignore[arg-type]
    )
    context.require(Permission.CHAT_USE)
    with pytest.raises(PermissionDeniedError):
        context.require(Permission.CHAT_USE, Permission.AUDIT_READ)


async def test_argon2id_production_parameters_and_verification() -> None:
    hasher = Argon2PasswordHasher()
    password_hash = await hasher.hash("correct horse battery staple")
    assert password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert await hasher.verify(password_hash, "correct horse battery staple")
    assert not await hasher.verify(password_hash, "wrong")
    assert not await hasher.verify("not-a-hash", "wrong")
    assert not hasher.needs_rehash(password_hash)
    # Unknown user: still verifies against a dummy hash and never succeeds.
    assert not await hasher.verify(None, "correct horse battery staple")


async def test_weaker_hashes_are_flagged_for_rehash() -> None:
    weak = await Argon2PasswordHasher(time_cost=1, memory_cost_kib=8, parallelism=1).hash("p" * 12 + "q")
    assert Argon2PasswordHasher().needs_rehash(weak)


async def test_hashing_does_not_block_the_event_loop() -> None:
    hasher = Argon2PasswordHasher()
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.005)

    task = asyncio.create_task(ticker())
    await hasher.hash("correct horse battery staple")
    task.cancel()
    assert ticks > 1
