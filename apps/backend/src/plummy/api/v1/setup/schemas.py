"""DTOs for ``/api/v1/setup`` (API_CONTRACTS.md §2.0). Requests reject unknown fields."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from plummy.api.v1.auth.schemas import MAX_EMAIL_INPUT_LENGTH, MAX_PASSWORD_INPUT_LENGTH


class SetupStatusResponse(BaseModel):
    status: Literal["uninitialized", "initialized"]
    web_setup_enabled: bool


class AdministratorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=MAX_EMAIL_INPUT_LENGTH)
    display_name: str = Field(min_length=1, max_length=400)
    password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_INPUT_LENGTH)


class InitializeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bootstrap_token: SecretStr = Field(min_length=1, max_length=256)
    admin: AdministratorRequest
