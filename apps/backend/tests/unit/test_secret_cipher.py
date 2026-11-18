"""AES-256-GCM secret cipher and key ring (ADR-016)."""

from __future__ import annotations

import base64
import json
import logging
import pickle
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from plummy.application.errors import InvalidInputError
from plummy.application.ports import EncryptedSecret, SecretDecryptionError
from plummy.application.secret_storage.vault import validate_secret
from plummy.composition import load_secret_cipher
from plummy.core.config import ConfigurationError
from plummy.core.ids import new_id
from plummy.domain.secrets import SecretKind, SecretValue, secret_hint
from plummy.infrastructure.security.secret_cipher import (
    ALGORITHM,
    AesGcmSecretCipher,
    KeyRing,
    KeyRingError,
    create_keyring_file,
    generate_key,
    load_keyring,
    parse_keyring,
)
from tests.conftest import make_settings

API_KEY = "sk-test-0123456789abcdefWXYZ"


def _key(fill: int) -> bytes:
    return bytes([fill]) * 32


def _encoded(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).rstrip(b"=").decode()


def _ring(primary: str = "k1", **keys: bytes) -> KeyRing:
    return KeyRing(primary=primary, keys=keys or {"k1": _key(1)})


def _document(primary: str, keys: dict[str, str]) -> bytes:
    return json.dumps({"primary": primary, "keys": keys}).encode()


# --- round trip ------------------------------------------------------------------------------------------


def test_encrypt_decrypt_round_trip() -> None:
    cipher = AesGcmSecretCipher(_ring())
    secret_id = new_id()

    encrypted = cipher.encrypt(secret_id, SecretKind.AI_API_KEY, SecretValue(API_KEY))

    assert encrypted.key_id == "k1"
    assert encrypted.algorithm == ALGORITHM == "AES-256-GCM"
    assert len(encrypted.nonce) == 12
    assert API_KEY.encode() not in encrypted.ciphertext
    assert cipher.decrypt(secret_id, SecretKind.AI_API_KEY, encrypted).reveal() == API_KEY


def test_every_encryption_uses_a_fresh_nonce() -> None:
    cipher = AesGcmSecretCipher(_ring())
    secret_id = new_id()
    first = cipher.encrypt(secret_id, SecretKind.AI_API_KEY, SecretValue(API_KEY))
    second = cipher.encrypt(secret_id, SecretKind.AI_API_KEY, SecretValue(API_KEY))
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext


def test_unicode_secret_round_trip() -> None:
    cipher = AesGcmSecretCipher(_ring())
    secret_id = new_id()
    value = "ключ-🔑-" + "x" * 20
    encrypted = cipher.encrypt(secret_id, SecretKind.OAUTH_CLIENT_SECRET, SecretValue(value))
    assert cipher.decrypt(secret_id, SecretKind.OAUTH_CLIENT_SECRET, encrypted).reveal() == value


# --- associated data and tampering -----------------------------------------------------------------------


def test_ciphertext_cannot_be_moved_to_another_record() -> None:
    cipher = AesGcmSecretCipher(_ring())
    encrypted = cipher.encrypt(new_id(), SecretKind.AI_API_KEY, SecretValue(API_KEY))
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(new_id(), SecretKind.AI_API_KEY, encrypted)


def test_ciphertext_cannot_be_reused_as_another_kind() -> None:
    cipher = AesGcmSecretCipher(_ring())
    secret_id = new_id()
    encrypted = cipher.encrypt(secret_id, SecretKind.AI_API_KEY, SecretValue(API_KEY))
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(secret_id, SecretKind.OAUTH_REFRESH_TOKEN, encrypted)


@pytest.mark.parametrize("part", ["ciphertext", "nonce"])
def test_tampered_bytes_are_detected(part: str) -> None:
    cipher = AesGcmSecretCipher(_ring())
    secret_id = new_id()
    encrypted = cipher.encrypt(secret_id, SecretKind.AI_API_KEY, SecretValue(API_KEY))
    original: bytes = getattr(encrypted, part)
    tampered = replace(encrypted, **{part: bytes([original[0] ^ 1]) + original[1:]})
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(secret_id, SecretKind.AI_API_KEY, tampered)


def test_unknown_key_id_and_algorithm_are_rejected() -> None:
    cipher = AesGcmSecretCipher(_ring())
    secret_id = new_id()
    encrypted = cipher.encrypt(secret_id, SecretKind.AI_API_KEY, SecretValue(API_KEY))
    with pytest.raises(SecretDecryptionError, match="unknown key id"):
        cipher.decrypt(secret_id, SecretKind.AI_API_KEY, replace(encrypted, key_id="gone"))
    with pytest.raises(SecretDecryptionError, match="unsupported algorithm"):
        cipher.decrypt(secret_id, SecretKind.AI_API_KEY, replace(encrypted, algorithm="AES-128-CBC"))
    with pytest.raises(SecretDecryptionError, match="invalid nonce"):
        cipher.decrypt(secret_id, SecretKind.AI_API_KEY, replace(encrypted, nonce=b"short"))


def test_same_key_id_with_different_key_material_fails() -> None:
    secret_id = new_id()
    encrypted = AesGcmSecretCipher(_ring(k1=_key(1))).encrypt(
        secret_id, SecretKind.AI_API_KEY, SecretValue(API_KEY)
    )
    with pytest.raises(SecretDecryptionError, match="authentication failed"):
        AesGcmSecretCipher(_ring(k1=_key(2))).decrypt(secret_id, SecretKind.AI_API_KEY, encrypted)


def test_old_keys_still_decrypt_after_primary_changes() -> None:
    secret_id = new_id()
    old = AesGcmSecretCipher(_ring("k1", k1=_key(1)))
    encrypted = old.encrypt(secret_id, SecretKind.AI_API_KEY, SecretValue(API_KEY))

    rotated = AesGcmSecretCipher(_ring("k2", k1=_key(1), k2=_key(2)))

    assert rotated.decrypt(secret_id, SecretKind.AI_API_KEY, encrypted).reveal() == API_KEY
    assert rotated.encrypt(secret_id, SecretKind.AI_API_KEY, SecretValue(API_KEY)).key_id == "k2"
    assert rotated.key_ids == frozenset({"k1", "k2"})


# --- nothing secret in repr / errors --------------------------------------------------------------------


def test_reprs_never_show_values_or_keys() -> None:
    ring = _ring(k1=_key(7))
    cipher = AesGcmSecretCipher(ring)
    value = SecretValue(API_KEY)
    encrypted = cipher.encrypt(new_id(), SecretKind.AI_API_KEY, value)
    rendered = " ".join([repr(ring), repr(cipher), repr(value), str(value), repr(encrypted), f"{value}"])
    assert API_KEY not in rendered
    assert _encoded(_key(7)) not in rendered
    assert repr(_key(7)) not in rendered
    assert repr(encrypted.ciphertext) not in rendered
    assert "k1" in rendered


def test_secret_value_cannot_be_pickled() -> None:
    with pytest.raises(TypeError):
        pickle.dumps(SecretValue(API_KEY))


@pytest.mark.parametrize(
    ("value", "hint"),
    [
        ("sk-0123456789abWXYZ", "WXYZ"),  # 19 characters
        ("a" * 15, None),  # shorter than 16: no hint
        ("b" * 16, "bbbb"),
    ],
)
def test_secret_hint(value: str, hint: str | None) -> None:
    assert secret_hint(SecretValue(value)) == hint


@pytest.mark.parametrize(
    ("value", "error_code"),
    [
        ("", "secret_empty"),
        ("   ", "secret_empty"),
        (" sk-padded", "secret_invalid"),
        ("sk-with\nnewline", "secret_invalid"),
        ("sk-\x00nul", "secret_invalid"),
        ("x" * 8193, "secret_too_long"),
    ],
)
def test_validate_secret_rejects_bad_values_without_echoing_them(value: str, error_code: str) -> None:
    with pytest.raises(InvalidInputError) as exc_info:
        validate_secret("api_key", value)
    assert exc_info.value.error_code == error_code
    assert exc_info.value.field == "api_key"
    if value.strip():
        assert value.strip() not in str(exc_info.value)


def test_validate_secret_accepts_maximum_length() -> None:
    assert len(validate_secret("api_key", "x" * 8192)) == 8192


# --- key ring parsing -----------------------------------------------------------------------------------


def test_parse_keyring_accepts_padded_and_unpadded_keys() -> None:
    padded = base64.urlsafe_b64encode(_key(3)).decode()
    ring = parse_keyring(_document("b", {"a": _encoded(_key(2)), "b": padded}))
    assert ring.primary == "b"
    assert ring.keys == {"a": _key(2), "b": _key(3)}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"not json", "not valid JSON"),
        (b"\xff\xfe", "not valid JSON"),
        (b"[]", "exactly 'primary' and 'keys'"),
        (json.dumps({"keys": {}}).encode(), "exactly 'primary' and 'keys'"),
        (json.dumps({"primary": "a", "keys": {}, "extra": 1}).encode(), "exactly 'primary' and 'keys'"),
        (json.dumps({"primary": 1, "keys": {}}).encode(), "'primary' must be a string"),
        (_document("a", {}), "no keys"),
        (_document("missing", {"a": _encoded(_key(1))}), "'primary' must name one of the keys"),
        (_document("a", {"a": _encoded(b"\x01" * 16)}), "32 bytes"),
        (_document("a", {"a": "!" * 43}), "32 bytes"),
        (_document("bad id!", {"bad id!": _encoded(_key(1))}), "key ids must be"),
        (_document("a" * 33, {"a" * 33: _encoded(_key(1))}), "key ids must be"),
        (b'{"primary": "a", "primary": "b", "keys": {}}', "duplicate entry"),
    ],
)
def test_invalid_keyrings_are_rejected_without_key_material(data: bytes, message: str) -> None:
    with pytest.raises(KeyRingError, match=message) as exc_info:
        parse_keyring(data)
    assert _encoded(_key(1)) not in str(exc_info.value)


def test_key_ring_error_does_not_echo_an_invalid_key() -> None:
    almost = _encoded(_key(9))[:-1] + "!"
    with pytest.raises(KeyRingError) as exc_info:
        parse_keyring(_document("a", {"a": almost}))
    assert almost not in str(exc_info.value)


# --- key ring files -------------------------------------------------------------------------------------


def test_create_keyring_file_is_private_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "keyring.json"

    assert create_keyring_file(path, "2026-11-18") is True
    first = path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    ring = load_keyring(path)
    assert ring.primary == "2026-11-18"
    assert len(ring.keys["2026-11-18"]) == 32

    assert create_keyring_file(path, "other") is False
    assert path.read_bytes() == first


def test_create_keyring_file_rejects_invalid_key_id(tmp_path: Path) -> None:
    with pytest.raises(KeyRingError):
        create_keyring_file(tmp_path / "keyring.json", "no spaces")
    assert not (tmp_path / "keyring.json").exists()


def test_generated_keys_are_random_and_valid() -> None:
    keys = {generate_key() for _ in range(5)}
    assert len(keys) == 5
    for key in keys:
        assert parse_keyring(_document("a", {"a": key})).keys["a"]


def test_load_keyring_reports_missing_and_non_regular_files(tmp_path: Path) -> None:
    with pytest.raises(KeyRingError, match="cannot read"):
        load_keyring(tmp_path / "missing.json")
    with pytest.raises(KeyRingError, match="not a regular file"):
        load_keyring(tmp_path)


def test_load_keyring_rejects_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "keyring.json"
    path.write_bytes(b" " * (64 * 1024 + 1))
    with pytest.raises(KeyRingError, match="larger than"):
        load_keyring(path)


def test_load_keyring_warns_about_wide_permissions(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "keyring.json"
    path.write_bytes(_document("a", {"a": _encoded(_key(5))}))
    path.chmod(0o644)
    with caplog.at_level(logging.WARNING):
        load_keyring(path)
    assert "recommended mode is 0400" in caplog.text
    assert _encoded(_key(5)) not in caplog.text


# --- composition ---------------------------------------------------------------------------------------


def test_load_secret_cipher_without_keyring_is_none() -> None:
    assert load_secret_cipher(make_settings()) is None


def test_load_secret_cipher_turns_keyring_errors_into_configuration_errors(tmp_path: Path) -> None:
    path = tmp_path / "keyring.json"
    path.write_bytes(_document("a", {"a": "short"}))
    with pytest.raises(ConfigurationError, match="SECRETS_KEYRING_FILE") as exc_info:
        load_secret_cipher(make_settings(secrets_keyring_file=path))
    assert "short" not in str(exc_info.value)


def test_load_secret_cipher_reads_the_keyring(tmp_path: Path) -> None:
    path = tmp_path / "keyring.json"
    create_keyring_file(path, "k1")
    cipher = load_secret_cipher(make_settings(secrets_keyring_file=path))
    assert cipher is not None
    assert cipher.primary_key_id == "k1"


def test_encrypted_secret_dataclass_hides_bytes() -> None:
    encrypted = EncryptedSecret(key_id="k", algorithm=ALGORITHM, nonce=b"\x01" * 12, ciphertext=b"\x02" * 40)
    assert "\\x02" not in repr(encrypted)
    assert "\\x01" not in repr(encrypted)


def test_check_configuration_fails_for_an_invalid_keyring(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from plummy.main import check_configuration

    path = tmp_path / "keyring.json"
    path.write_text("not json")
    clean_env.setenv("APP_ENV", "development")
    clean_env.setenv("DATABASE_URL", "postgresql+asyncpg://plummy:x@db/plummy")
    clean_env.setenv("REDIS_URL", "redis://redis:6379/0")
    clean_env.setenv("SECRETS_KEYRING_FILE", str(path))
    assert check_configuration() == 1
    create_keyring_file(tmp_path / "valid.json", "k1")
    clean_env.setenv("SECRETS_KEYRING_FILE", str(tmp_path / "valid.json"))
    from plummy.core.config import get_settings

    get_settings.cache_clear()
    assert check_configuration() == 0
