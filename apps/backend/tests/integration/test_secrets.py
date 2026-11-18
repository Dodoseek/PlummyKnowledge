"""Secrets at rest on real PostgreSQL (ADR-016): vault, rotation, readiness, CLI."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path

import pytest

from plummy.application.secret_storage.use_cases import RotateSecrets
from plummy.application.secret_storage.vault import (
    SecretsNotConfiguredError,
    SecretUnavailableError,
    SecretVault,
)
from plummy.cli.app import main
from plummy.composition import build_cli_services
from plummy.domain.secrets import SecretKind, SecretValue
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from plummy.infrastructure.security.secret_cipher import (
    AesGcmSecretCipher,
    generate_key,
    keyring_document,
    load_keyring,
)
from tests.integration.harness import Harness, fast_hasher, harness_settings

pytestmark = pytest.mark.integration

API_KEY = "sk-live-0123456789abcdefWXYZ"


def _uow(harness: Harness) -> SqlAlchemyUnitOfWorkFactory:
    return SqlAlchemyUnitOfWorkFactory(harness.resources.session_factory)


def _cipher(path: Path) -> AesGcmSecretCipher:
    return AesGcmSecretCipher(load_keyring(path))


def _add_key(path: Path, new_key_id: str) -> None:
    """What the operator does before rotation: add a key and make it primary (file stays mode 0400)."""
    document = json.loads(path.read_text())
    document["keys"][new_key_id] = generate_key()
    path.chmod(0o600)
    path.write_text(keyring_document(new_key_id, document["keys"]))
    path.chmod(0o400)


async def _store(harness: Harness, vault: SecretVault, value: str = API_KEY) -> str:
    uow_factory = _uow(harness)
    async with uow_factory() as uow:
        record = await vault.store(uow, SecretKind.AI_API_KEY, SecretValue(value))
        await uow.commit()
    return str(record.id)


async def _reveal(harness: Harness, vault: SecretVault, secret_id: str) -> str:
    from uuid import UUID

    async with _uow(harness)() as uow:
        return (await vault.reveal(uow, UUID(secret_id))).reveal()


# --- vault -----------------------------------------------------------------------------------------------


async def test_vault_stores_only_ciphertext(harness: Harness) -> None:
    vault = harness.resources.secrets
    secret_id = await _store(harness, vault)

    rows = await harness.sql(
        "SELECT kind, backend, key_id, algorithm, hint, octet_length(nonce), ciphertext, rotated_at "
        "FROM secret_references WHERE id = :id",
        id=secret_id,
    )
    kind, backend, key_id, algorithm, hint, nonce_length, ciphertext, rotated_at = rows[0]
    assert (kind, backend, key_id, algorithm, hint, nonce_length) == (
        "ai_api_key",
        "db_encrypted",
        "test-1",
        "AES-256-GCM",
        "WXYZ",
        12,
    )
    assert API_KEY.encode() not in bytes(ciphertext)
    assert rotated_at is None
    assert await _reveal(harness, vault, secret_id) == API_KEY


async def test_vault_replace_writes_new_ciphertext(harness: Harness) -> None:
    from uuid import UUID

    vault = harness.resources.secrets
    secret_id = await _store(harness, vault)
    before = await harness.sql("SELECT nonce, ciphertext FROM secret_references WHERE id = :id", id=secret_id)

    async with _uow(harness)() as uow:
        await vault.replace(uow, UUID(secret_id), SecretValue("short-key"))
        await uow.commit()

    after = await harness.sql(
        "SELECT nonce, ciphertext, hint FROM secret_references WHERE id = :id", id=secret_id
    )
    assert after[0][0] != before[0][0]
    assert after[0][1] != before[0][1]
    assert after[0][2] is None  # shorter than 16 characters: no hint
    assert await _reveal(harness, vault, secret_id) == "short-key"


async def test_vault_rejects_moved_or_relabelled_ciphertext(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    vault = harness.resources.secrets
    first = await _store(harness, vault)
    second = await _store(harness, vault, "sk-other-0123456789abcdefABCD")

    # Swap the ciphertexts between rows (an attacker with write access to the database).
    await harness.sql(
        "UPDATE secret_references SET ciphertext = (SELECT ciphertext FROM secret_references WHERE id = :b), "
        "nonce = (SELECT nonce FROM secret_references WHERE id = :b) WHERE id = :a",
        a=first,
        b=second,
    )
    with caplog.at_level(logging.ERROR), pytest.raises(SecretUnavailableError):
        await _reveal(harness, vault, first)

    await harness.sql("UPDATE secret_references SET kind = 'oauth_client_secret' WHERE id = :id", id=second)
    with caplog.at_level(logging.ERROR), pytest.raises(SecretUnavailableError):
        await _reveal(harness, vault, second)

    assert "secret decryption failed" in caplog.text
    assert API_KEY not in caplog.text
    assert "sk-other" not in caplog.text
    failures = [record for record in caplog.records if record.getMessage() == "secret decryption failed"]
    # Only identifiers are logged: the record id, the key id and the error type.
    assert {getattr(record, "secret_id", None) for record in failures} == {first, second}
    assert {getattr(record, "key_id", None) for record in failures} == {"test-1"}
    for record in failures:
        assert API_KEY not in json.dumps(record.__dict__, default=str)


async def test_vault_unknown_secret_is_unavailable(harness: Harness) -> None:
    with pytest.raises(SecretUnavailableError):
        await _reveal(harness, harness.resources.secrets, "00000000-0000-0000-0000-000000000000")


async def test_vault_without_keyring_refuses_to_store(harness: Harness) -> None:
    vault = SecretVault(None, SystemClock())
    assert vault.configured is False
    async with _uow(harness)() as uow:
        with pytest.raises(SecretsNotConfiguredError):
            await vault.store(uow, SecretKind.AI_API_KEY, SecretValue(API_KEY))
    assert await harness.sql("SELECT count(*) FROM secret_references") == [(0,)]


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("kind", "'password'"),
        ("backend", "'vault'"),
        ("algorithm", "'AES-128-CBC'"),
        ("nonce", "'\\x0102'::bytea"),
        ("key_id", "'bad id'"),
        ("hint", "'abcdef'"),
        ("ciphertext", "'\\x00'::bytea"),
    ],
)
async def test_database_constraints_reject_malformed_rows(harness: Harness, column: str, value: str) -> None:
    from sqlalchemy.exc import IntegrityError

    secret_id = await _store(harness, harness.resources.secrets)
    with pytest.raises(IntegrityError):
        await harness.sql(f"UPDATE secret_references SET {column} = {value} WHERE id = :id", id=secret_id)  # noqa: S608


# --- rotation --------------------------------------------------------------------------------------------


async def test_rotation_reencrypts_everything_and_is_idempotent(harness: Harness) -> None:
    keyring_file = harness.settings.secrets_keyring_file
    assert keyring_file is not None
    old_vault = harness.resources.secrets
    ids = [await _store(harness, old_vault, f"sk-value-{index:02d}-0123456789") for index in range(5)]

    _add_key(keyring_file, "test-2")
    cipher = _cipher(keyring_file)
    # Small batches exercise the keyset pagination over ids.
    rotate = RotateSecrets(_uow(harness), cipher, SystemClock(), batch_size=2)

    report = await rotate.execute()
    assert (report.primary_key_id, report.rotated, report.failed) == ("test-2", 5, 0)
    rows = await harness.sql("SELECT key_id, rotated_at IS NOT NULL FROM secret_references")
    assert set(rows) == {("test-2", True)}
    new_vault = SecretVault(cipher, SystemClock())
    for index, secret_id in enumerate(ids):
        assert await _reveal(harness, new_vault, secret_id) == f"sk-value-{index:02d}-0123456789"

    again = await rotate.execute()
    assert (again.rotated, again.failed) == (0, 0)

    audit = await harness.sql(
        "SELECT actor_type, outcome, details FROM audit_logs "
        "WHERE action = 'secrets.rotated' ORDER BY created_at"
    )
    assert [(row[0], row[1]) for row in audit] == [("system", "success"), ("system", "success")]
    assert audit[0][2] == {"primary_key_id": "test-2", "rotated": 5, "failed": 0}
    assert API_KEY not in json.dumps([row[2] for row in audit])


async def test_rotation_continues_past_undecryptable_records(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    keyring_file = harness.settings.secrets_keyring_file
    assert keyring_file is not None
    good = await _store(harness, harness.resources.secrets)
    broken = await _store(harness, harness.resources.secrets, "sk-broken-0123456789abcd")
    await harness.sql("UPDATE secret_references SET kind = 'oauth_refresh_token' WHERE id = :id", id=broken)

    _add_key(keyring_file, "test-2")
    with caplog.at_level(logging.ERROR):
        report = await RotateSecrets(_uow(harness), _cipher(keyring_file), SystemClock()).execute()

    assert (report.rotated, report.failed) == (1, 1)
    keys = dict(await harness.sql("SELECT id::text, key_id FROM secret_references"))
    assert keys == {good: "test-2", broken: "test-1"}
    assert "secret rotation failed" in caplog.text
    assert "sk-broken" not in caplog.text
    outcome = await harness.sql("SELECT outcome FROM audit_logs WHERE action = 'secrets.rotated'")
    assert outcome == [("failure",)]


async def test_concurrent_rotations_do_not_double_count(harness: Harness) -> None:
    keyring_file = harness.settings.secrets_keyring_file
    assert keyring_file is not None
    for index in range(6):
        await _store(harness, harness.resources.secrets, f"sk-concurrent-{index}-0123456789")
    _add_key(keyring_file, "test-2")
    cipher = _cipher(keyring_file)

    first, second = await asyncio.gather(
        RotateSecrets(_uow(harness), cipher, SystemClock()).execute(),
        RotateSecrets(_uow(harness), cipher, SystemClock()).execute(),
    )

    assert first.rotated + second.rotated == 6
    assert first.failed == second.failed == 0
    assert await harness.sql("SELECT DISTINCT key_id FROM secret_references") == [("test-2",)]


# --- readiness -------------------------------------------------------------------------------------------


async def test_readiness_fails_when_a_key_is_missing_from_the_keyring(harness: Harness) -> None:
    await _store(harness, harness.resources.secrets)
    ready = await harness.client.get("/api/v1/health/ready")
    assert ready.status_code == 200

    await harness.sql("UPDATE secret_references SET key_id = 'removed-key'")
    ready = await harness.client.get("/api/v1/health/ready")
    assert ready.status_code == 503
    assert ready.json()["checks"]["secrets"] == "fail"
    assert "removed-key" not in ready.text


async def test_readiness_fails_for_stored_secrets_without_a_keyring(
    harness: Harness, migrated_database_url: str, redis_url: str, tmp_path: Path
) -> None:
    from tests.integration.harness import running_auth_app

    await _store(harness, harness.resources.secrets)
    async with running_auth_app(
        migrated_database_url, redis_url, tmp_path / "other" / "bootstrap", secrets_keyring_file=None
    ) as without_keyring:
        ready = await without_keyring.client.get("/api/v1/health/ready")
    assert ready.status_code == 503
    assert ready.json()["checks"]["secrets"] == "fail"


async def test_invalid_keyring_stops_startup(
    migrated_database_url: str, redis_url: str, tmp_path: Path
) -> None:
    from plummy.composition import build_resources
    from plummy.core.config import ConfigurationError

    keyring = tmp_path / "keyring.json"
    keyring.write_text('{"primary": "a", "keys": {"a": "too-short"}}')
    settings = harness_settings(
        migrated_database_url, redis_url, tmp_path / "bootstrap", secrets_keyring_file=keyring
    )
    with pytest.raises(ConfigurationError, match="SECRETS_KEYRING_FILE") as exc_info:
        build_resources(settings, password_hasher=fast_hasher())
    assert "too-short" not in str(exc_info.value)


# --- CLI -------------------------------------------------------------------------------------------------


def _cli(harness: Harness, *argv: str, **settings_overrides: object) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    settings = (
        harness.settings.model_copy(update=settings_overrides) if settings_overrides else harness.settings
    )
    code = main(
        list(argv),
        services_factory=lambda s: build_cli_services(s, password_hasher=fast_hasher()),
        settings=settings,
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


async def test_cli_status_and_rotate(harness: Harness) -> None:
    keyring_file = harness.settings.secrets_keyring_file
    assert keyring_file is not None
    await _store(harness, harness.resources.secrets)

    code, out, _ = await asyncio.to_thread(_cli, harness, "secrets", "status")
    assert code == 0
    assert "key ring: 1 key(s), primary test-1" in out
    assert "test-1: 1 (primary)" in out
    assert "on old keys: 0" in out

    _add_key(keyring_file, "test-2")
    code, out, _ = await asyncio.to_thread(_cli, harness, "secrets", "status")
    assert code == 0
    assert "on old keys: 1" in out

    code, out, _ = await asyncio.to_thread(_cli, harness, "secrets", "rotate")
    assert code == 0, out
    assert "Rotated 1 secret(s) to key test-2; 0 failed." in out
    code, out, _ = await asyncio.to_thread(_cli, harness, "secrets", "rotate")
    assert "Rotated 0 secret(s)" in out

    key_material = json.loads(keyring_file.read_text())["keys"]
    for key in key_material.values():
        assert key not in out


async def test_cli_status_fails_for_keys_missing_from_the_keyring(harness: Harness) -> None:
    await _store(harness, harness.resources.secrets)
    await harness.sql("UPDATE secret_references SET key_id = 'removed-key'")
    code, out, err = await asyncio.to_thread(_cli, harness, "secrets", "status")
    assert code == 1
    assert "removed-key: 1 (MISSING from the key ring" in out
    assert "missing from the key ring" in err


async def test_cli_rotate_reports_failures(harness: Harness) -> None:
    keyring_file = harness.settings.secrets_keyring_file
    assert keyring_file is not None
    broken = await _store(harness, harness.resources.secrets)
    await harness.sql("UPDATE secret_references SET kind = 'oauth_refresh_token' WHERE id = :id", id=broken)
    _add_key(keyring_file, "test-2")
    code, out, err = await asyncio.to_thread(_cli, harness, "secrets", "rotate")
    assert code == 1
    assert "0 secret(s)" not in err
    assert "1 secret(s) could not be decrypted" in err
    assert API_KEY not in out + err


async def test_cli_without_keyring(harness: Harness) -> None:
    code, out, _ = await asyncio.to_thread(_cli, harness, "secrets", "status", secrets_keyring_file=None)
    assert code == 0
    assert "not configured" in out
    code, _, err = await asyncio.to_thread(_cli, harness, "secrets", "rotate", secrets_keyring_file=None)
    assert code == 1
    assert "SECRETS_KEYRING_FILE is not set" in err
    code, _, err = await asyncio.to_thread(
        _cli, harness, "secrets", "init-keyring", secrets_keyring_file=None
    )
    assert code == 1
    assert "SECRETS_KEYRING_FILE is not set" in err


async def test_cli_init_keyring_creates_once_and_never_prints_the_key(
    harness: Harness, tmp_path: Path
) -> None:
    path = tmp_path / "volume" / "keyring.json"

    code, out, _ = await asyncio.to_thread(
        _cli, harness, "secrets", "init-keyring", "--key-id", "dev-1", secrets_keyring_file=path
    )
    assert code == 0
    assert "Key ring created" in out
    key = json.loads(path.read_text())["keys"]["dev-1"]
    assert key not in out

    code, out, _ = await asyncio.to_thread(
        _cli, harness, "secrets", "init-keyring", secrets_keyring_file=path
    )
    assert code == 0
    assert "already exists" in out
    assert json.loads(path.read_text())["keys"] == {"dev-1": key}


async def test_cli_init_keyring_rejects_an_invalid_existing_file(harness: Harness, tmp_path: Path) -> None:
    path = tmp_path / "invalid" / "keyring.json"
    path.parent.mkdir()
    path.write_text("{}")
    code, _, err = await asyncio.to_thread(
        _cli, harness, "secrets", "init-keyring", secrets_keyring_file=path
    )
    assert code == 1
    assert "key ring:" in err
    assert path.read_text() == "{}"


def test_cli_generate_key_prints_a_valid_entry() -> None:
    out, err = io.StringIO(), io.StringIO()
    code = main(["secrets", "generate-key", "--key-id", "2027-01"], stdout=out, stderr=err)
    assert code == 0
    entry = json.loads(out.getvalue())
    assert list(entry) == ["2027-01"]
    keyring = json.dumps({"primary": "2027-01", "keys": entry}).encode()
    from plummy.infrastructure.security.secret_cipher import parse_keyring

    assert parse_keyring(keyring).primary == "2027-01"
    assert "secrets rotate" in err.getvalue()


def test_cli_rejects_invalid_key_ids() -> None:
    with pytest.raises(SystemExit):
        main(["secrets", "generate-key", "--key-id", "bad id"], stdout=io.StringIO(), stderr=io.StringIO())
