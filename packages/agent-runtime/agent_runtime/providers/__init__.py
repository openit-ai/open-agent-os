"""Providers registry — lazy dispatch for LLMProviderAdapter.

Each provider exposes `call(messages, model, tools, **kwargs) -> dict` returning
OpenAI-compatible chat.completion dict. Lazy imports + mock fallback so no hard deps.
"""
from __future__ import annotations

from typing import Any

from .claude import ClaudeProvider
from .codex import CodexProvider
from .gemini import GeminiProvider
from .opencode import OpenCodeProvider
from .ollama import OllamaProvider

__all__ = [
    "ClaudeProvider",
    "CodexProvider",
    "GeminiProvider",
    "OpenCodeProvider",
    "OllamaProvider",
    "PROVIDER_MAP",
    "get_provider",
]

PROVIDER_MAP: dict[str, Any] = {
    "claude": ClaudeProvider,
    "codex": CodexProvider,
    "gemini": GeminiProvider,
    "opencode": OpenCodeProvider,
    "ollama": OllamaProvider,
}

def get_provider(provider_type: str, config: dict[str, Any] | None = None) -> Any:
    """Instantiate provider by type string (case-insensitive)."""
    key = str(provider_type).lower()
    klass = PROVIDER_MAP.get(key)
    if klass is None:
        raise ValueError(f"Unknown provider: {provider_type!r} (known: {list(PROVIDER_MAP)})")
    return klass(**(config or {}))
