"""Model override guard — validates persisted /model overrides before rehydration.

Hermes gateway persists session model/provider/base_url to sessions.json via
sanitize_model_override and rehydrates via _rehydrate_session_model_override.
A bug: any persisted string (e.g. model=gpt-5.6-luna provider=custom) is
blindly re-applied, overriding config.yaml primary model even when the
provider is not configured and the model does not exist.

This module is the OAOS-side fix (and documents the same fix for Hermes
proper). It validates a persisted override against the configured primary
and the set of known/configured providers before allowing it to shadow the
primary. Explicit valid user overrides are preserved.

Usage mirrors Hermes semantics:
  - Bare ``custom`` provider without a named custom_providers entry is invalid.
  - Unknown provider not in ProviderType / configured set is invalid.
  - Model ``gpt-5.6-*`` family with ``custom`` provider is invalid (hallucinated).
  - Valid overrides pass through unchanged.

No Hermes source is mutated here; this is the OAOS integration guard.
"""

from __future__ import annotations

import os
import re
from typing import Any

# Providers known to OAOS + Hermes core (lowercased).
_KNOWN_CORE_PROVIDERS: set[str] = {
    "opencode-go",
    "opencode",
    "claude",
    "codex",
    "gemini",
    "openrouter",
    "ollama",
    "litellm",  # Hermes litellm local gateway
    "hermes",
    "safe",
    "openai",
    "openai-codex",
    "anthropic",
    "google",
    "vertex",
    "bedrock",
}

# Models that are known-invalid when paired with custom provider.
# Production bug: gpt-5.6-luna provider=custom was persisted and rehydrated.
_INVALID_MODEL_RE = re.compile(r"^gpt-5\.6", re.IGNORECASE)
_INVALID_MODELS_EXACT: set[str] = {
    "gpt-5.6-luna",
    "gpt-5.6",
}


def _normalize_provider(p: Any) -> str:
    return str(p or "").strip().lower().replace(" ", "-")


def _custom_provider_ids(custom_providers: list[dict[str, Any]] | None) -> set[str]:
    """Return normalized ids that runtime accepts for configured custom providers."""
    if not custom_providers:
        return set()
    ids: set[str] = set()
    for e in custom_providers:
        if not isinstance(e, dict):
            continue
        for k in ("provider_key", "name", "provider", "id"):
            v = e.get(k)
            if v:
                n = _normalize_provider(v)
                ids.add(n)
                ids.add(f"custom:{n}")
    return ids


def is_valid_provider(
    provider: str | None,
    *,
    valid_providers: set[str] | None = None,
    custom_providers: list[dict[str, Any]] | None = None,
) -> bool:
    """Return True iff provider is known/configured.

    - Empty/None is treated as missing -> invalid for persisted override
      (caller should fall back to primary).
    - ``custom`` bare with no custom_providers is invalid.
    - ``custom:<name>`` is valid only if <name> is in custom_providers.
    - Otherwise provider must be in KNOWN_CORE_PROVIDERS or valid_providers.
    """
    if not provider:
        return False
    p = _normalize_provider(provider)
    if p == "custom":
        # bare custom is never valid unless caller explicitly allows it
        # (Hermes requires a named custom provider).
        if custom_providers:
            # if any custom provider is configured, bare custom is still ambiguous;
            # treat as invalid — caller must use custom:<name>.
            return False
        return False
    if p.startswith("custom:"):
        # need a matching custom provider entry
        if custom_providers is None:
            # no allowlist -> be conservative: only allow if valid_providers contains it
            if valid_providers is not None:
                return p in {x.lower() for x in valid_providers}
            return False
        ids = _custom_provider_ids(custom_providers)
        return p in ids or p.split(":", 1)[1] in ids
    # core provider
    if valid_providers is not None:
        lowered = {x.lower().replace(" ", "-") for x in valid_providers}
        return p in lowered
    return p in _KNOWN_CORE_PROVIDERS


def _is_valid_provider_strict(
    provider: str | None,
    valid_providers: set[str] | None,
    custom_providers: list[dict[str, Any]] | None,
) -> bool:
    if valid_providers is not None:
        # strict: must be in the provided set (case-insensitive)
        if not provider:
            return False
        p = _normalize_provider(provider)
        lowered = {x.lower().replace(" ", "-") for x in valid_providers}
        # custom:<name> handling
        if p.startswith("custom:"):
            if custom_providers is not None:
                ids = _custom_provider_ids(custom_providers)
                return p in ids
            return p in lowered
        return p in lowered
    return is_valid_provider(provider, valid_providers=valid_providers, custom_providers=custom_providers)


def is_valid_model_for_provider(model: str | None, provider: str | None) -> bool:
    """Reject known hallucinated models (gpt-5.6 family on custom)."""
    if not model:
        return False
    m = str(model).strip()
    if not m:
        return False
    prov = _normalize_provider(provider)
    if prov == "custom" or prov.startswith("custom:"):
        if m.lower() in _INVALID_MODELS_EXACT:
            return False
        if _INVALID_MODEL_RE.match(m):
            return False
    # bare custom provider + exact invalid model (redundant but explicit)
    if prov == "custom" and m.lower() in _INVALID_MODELS_EXACT:
        return False
    return True


def is_persisted_override_valid(
    override: dict[str, Any] | None,
    *,
    valid_providers: set[str] | None = None,
    custom_providers: list[dict[str, Any]] | None = None,
    primary_model: str | None = None,
    primary_provider: str | None = None,
) -> bool:
    """Validate a persisted /model override dict.

    Returns True only if both provider and model pass validation.
    primary_* are unused for validity but kept for API symmetry / future
    allowlist checks (model must differ from primary is NOT required).
    """
    if not isinstance(override, dict) or not override:
        return False
    provider = override.get("provider")
    model = override.get("model")
    if not is_valid_model_for_provider(model, provider):
        return False
    # provider must be explicitly valid
    if valid_providers is not None:
        if not _is_valid_provider_strict(provider, valid_providers, custom_providers):
            return False
    else:
        if not is_valid_provider(provider, valid_providers=None, custom_providers=custom_providers):
            return False
    return True


def resolve_effective_model(
    persisted_override: dict[str, Any] | None,
    *,
    primary_model: str,
    primary_provider: str,
    valid_providers: set[str] | None = None,
    custom_providers: list[dict[str, Any]] | None = None,
    primary_base_url: str | None = None,
) -> tuple[str, str, str | None]:
    """Return (effective_model, effective_provider, effective_base_url).

    If persisted_override is valid, its values win (preserving explicit
    valid user overrides). Otherwise fallback to primary (safe default).
    base_url is taken from override if present, else primary.
    """
    if is_persisted_override_valid(
        persisted_override,
        valid_providers=valid_providers,
        custom_providers=custom_providers,
        primary_model=primary_model,
        primary_provider=primary_provider,
    ):
        assert persisted_override is not None
        eff_model = str(persisted_override.get("model") or primary_model).strip() or primary_model
        eff_provider = str(persisted_override.get("provider") or primary_provider).strip() or primary_provider
        eff_base = persisted_override.get("base_url") or primary_base_url
        return eff_model, eff_provider, eff_base
    return primary_model, primary_provider, primary_base_url


def sanitize_and_validate_override(
    override: dict[str, Any] | None,
    *,
    valid_providers: set[str] | None = None,
    custom_providers: list[dict[str, Any]] | None = None,
) -> dict[str, str] | None:
    """Sanitize (persistable keys only) then validate. Return None if invalid."""
    if not isinstance(override, dict):
        return None
    # Only persistable keys (mirror Hermes gateway/session.py)
    persistable: dict[str, str] = {}
    for k in ("model", "provider", "base_url"):
        v = override.get(k)
        if v not in (None, ""):
            persistable[k] = str(v)
    if not persistable.get("model") or not persistable.get("provider"):
        return None
    if not is_persisted_override_valid(
        persistable, valid_providers=valid_providers, custom_providers=custom_providers
    ):
        return None
    return persistable


# ---------------------------------------------------------------------------
# Helpers to derive valid provider set from OAOS/Hermes config
# ---------------------------------------------------------------------------

def valid_providers_from_env() -> set[str]:
    """Derive valid provider set from environment / OAOS config.

    OAOS primary is env-driven; Hermes primary is config.yaml.
    This helper collects plausible valid providers for the guard default.
    """
    providers: set[str] = set()
    # OAOS LLM provider
    for k in ("OAOS_LLM_PROVIDER", "LLM_PROVIDER", "OAOS_PROVIDER", "PROVIDER_TYPE"):
        v = os.getenv(k)
        if v:
            providers.add(_normalize_provider(v))
    # Hermes primary provider from env (if any)
    for k in ("HERMES_PROVIDER",):
        v = os.getenv(k)
        if v:
            providers.add(_normalize_provider(v))
    # Always include core providers that are plausibly configured;
    # actual strict set should be passed by caller.
    return providers


def hermes_primary_from_config(config: dict[str, Any] | None) -> tuple[str, str, str | None]:
    """Extract (model, provider, base_url) from a Hermes-style config dict."""
    if not config or not isinstance(config, dict):
        return "", "", None
    raw = config.get("model")
    model_cfg: dict[str, Any] = raw if isinstance(raw, dict) else {}
    model = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
    provider = str(model_cfg.get("provider") or "").strip()
    base_url = model_cfg.get("base_url")
    return model, provider, str(base_url).strip() if base_url else None
