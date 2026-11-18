"""Input rules shared by web setup and the server CLI."""

from __future__ import annotations

from dataclasses import dataclass

from plummy.application.errors import InvalidInputError
from plummy.domain.identity import (
    InvalidDisplayNameError,
    InvalidEmailError,
    PasswordPolicyError,
    check_password_policy,
    normalize_display_name,
    normalize_email,
)


@dataclass(frozen=True, slots=True)
class AdministratorInput:
    email: str
    display_name: str
    password: str


def validate_email(value: str, *, field: str) -> str:
    try:
        return normalize_email(value)
    except InvalidEmailError as exc:
        raise InvalidInputError(field, "invalid_email", str(exc)) from None


def validate_password(password: str, *, email: str, field: str) -> None:
    try:
        check_password_policy(password, email=email)
    except PasswordPolicyError as exc:
        raise InvalidInputError(field, exc.code, exc.message) from None


def validate_administrator(raw: AdministratorInput, *, field_prefix: str = "") -> AdministratorInput:
    email = validate_email(raw.email, field=f"{field_prefix}email")
    try:
        display_name = normalize_display_name(raw.display_name)
    except InvalidDisplayNameError as exc:
        raise InvalidInputError(f"{field_prefix}display_name", "invalid_display_name", str(exc)) from None
    validate_password(raw.password, email=email, field=f"{field_prefix}password")
    return AdministratorInput(email=email, display_name=display_name, password=raw.password)
