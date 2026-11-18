"""AI provider vocabulary shared by the network policy, adapters and provider configuration."""

from __future__ import annotations

from enum import StrEnum


class ProviderType(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"
