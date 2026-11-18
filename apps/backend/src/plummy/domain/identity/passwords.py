"""Password policy (ADR-013 §3, SECURITY_MODEL.md §4.1): length 12-128, no composition rules."""

from __future__ import annotations

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128


class PasswordPolicyError(ValueError):
    """The password violates the policy. ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def check_password_policy(password: str, *, email: str | None = None) -> None:
    """Raise :class:`PasswordPolicyError` if the password is not acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            "password_too_short", f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            "password_too_long", f"Password must be at most {MAX_PASSWORD_LENGTH} characters long."
        )
    if len(set(password)) == 1:
        raise PasswordPolicyError("password_repetitive", "Password must not repeat a single character.")
    if email is not None and password.strip().casefold() == email.strip().casefold():
        raise PasswordPolicyError("password_matches_email", "Password must not be the same as the email.")
