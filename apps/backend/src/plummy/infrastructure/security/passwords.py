"""Argon2id password hashing (ADR-013 §3). CPU/memory-heavy work runs in a worker thread."""

from __future__ import annotations

import asyncio
import secrets

from argon2 import PasswordHasher as _Argon2
from argon2 import Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# RFC 9106 "second recommended option" (low memory): t=3, m=64 MiB, p=4.
TIME_COST = 3
MEMORY_COST_KIB = 64 * 1024
PARALLELISM = 4


class Argon2PasswordHasher:
    def __init__(
        self,
        *,
        time_cost: int = TIME_COST,
        memory_cost_kib: int = MEMORY_COST_KIB,
        parallelism: int = PARALLELISM,
    ) -> None:
        self._hasher = _Argon2(
            time_cost=time_cost,
            memory_cost=memory_cost_kib,
            parallelism=parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._dummy_hash: str | None = None
        self._dummy_lock = asyncio.Lock()

    async def hash(self, password: str) -> str:
        return await asyncio.to_thread(self._hasher.hash, password)

    async def verify(self, password_hash: str | None, password: str) -> bool:
        if password_hash is None:
            # Unknown user: spend the same effort so timing does not reveal whether the account exists.
            await asyncio.to_thread(self._verify_sync, await self._dummy(), password)
            return False
        return await asyncio.to_thread(self._verify_sync, password_hash, password)

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True

    def _verify_sync(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    async def _dummy(self) -> str:
        if self._dummy_hash is None:
            async with self._dummy_lock:
                if self._dummy_hash is None:
                    self._dummy_hash = await self.hash(secrets.token_urlsafe(32))
        return self._dummy_hash
