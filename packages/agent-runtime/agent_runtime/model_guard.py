"""Model/ provider guard — OAOS-side defense-in-depth for persisted session overrides.

Hermes production fix is authoritative (see docs/HERMES_PRODUCTION_REMEDIATION.md).
This module provides an OAOS-side safety net so a stale persisted session
that contains a misconfigured `custom` / loopback provider (e.g. gpt-5.6-luna at
127.0.0.1:10100) can never override the default model or block the primary
provider (muse-spark via opencode-go).  All OAOS entry points must sanitize
any provider/model/base_url coming from session metadata, user request, or
external config before persisting or honoring it.

Allowed providers are intentionally small and mirror admin-console/backend/fallback.py:
  claude, codex, gemini, opencode-go (+alias opencode), openrouter, ollama
No `custom` — that alias is Hermes-internal and must not be trusted from
persisted OAOS session state.

Block list:
  - provider == "custom" (any case) → blocked
  - model substring "gpt-5.6-luna" or "gpt-5.6-sol" (case-insensitive)
  - base_url substring "127.0.0.1:10100" or "localhost:10100"

Sanitization is non-throwing: invalid entries are stripped / rejected with a
warning telemetry call, never propagated.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

ALLOWED_PROVIDERS = {"claude", "codex", "gemini", "opencode-go", "openrouter", "ollama"}
OPENSPEC_ALIAS = {"opencode": "opencode-go"}
BLOCKED_MODEL_SUBSTRINGS = ("gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6")
BLOCKED_URL_SUBSTRINGS = ("127.0.0.1:10100", "localhost:10100")

_PERSISTED_OVERRIDE_KEYS = (
    "model_override",
    "preferred_model",
    "model",
    "provider",
    "base_url",
    "baseUrl",
    "provider_model",
    "custom_provider",
)


def _normalize_provider(p: str | None) -> str | None:
    if not p:
        return None
    s = str(p).strip().lower()
    if not s:
        return None
    if s in OPENSPEC_ALIAS:
        s = OPENSPEC_ALIAS[s]
    return s


def is_allowed_provider(provider: str | None) -> bool:
    n = _normalize_provider(provider)
    if n is None:
        return False
    return n in ALLOWED_PROVIDERS


def is_blocked_model(model: str | None) -> bool:
    if not model:
        return False
    low = str(model).lower()
    return any(sub in low for sub in BLOCKED_MODEL_SUBSTRINGS)


def is_blocked_base_url(url: str | None) -> bool:
    if not url:
        return False
    low = str(url).lower()
    return any(sub in low for sub in BLOCKED_URL_SUBSTRINGS)


def is_blocked_entry(entry: dict[str, Any] | Any) -> tuple[bool, str]:
    """Return (blocked, reason) for a provider/model/base_url entry dict.
    Accepts dicts of shape {"provider":..., "model":..., "base_url":...} or bare strings.
    """
    if entry is None:
        return False, ""
    if isinstance(entry, str):
        if is_blocked_model(entry):
            return True, f"blocked model substring in '{entry}'"
        return False, ""
    if not isinstance(entry, dict):
        return False, ""
    provider = entry.get("provider") or entry.get("provider_type") or ""
    model = entry.get("model") or entry.get("model_name") or ""
    base_url = entry.get("base_url") or entry.get("baseUrl") or entry.get("baseURL") or ""
    # provider custom always blocked even if model looks benign
    nprov = _normalize_provider(str(provider) if provider else "")
    if nprov == "custom" or str(provider).strip().lower() == "custom":
        return True, f"blocked provider 'custom' (model={model!r})"
    if provider and nprov is not None and nprov not in ALLOWED_PROVIDERS and str(provider).strip():
        # unknown provider is blocked (fail-closed); allow only allowlist
        # but don't block empty provider or internal bookkeeping providers
        if str(provider).strip().lower() not in ALLOWED_PROVIDERS:
            # Check if it's an empty/missing provider — don't block if no provider specified
            if str(provider).strip().lower() not in ("", "unknown", "hermes", "safe", "default"):
                return True, f"provider not in allowlist: {provider!r}"
    if is_blocked_model(str(model) if model else None):
        return True, f"blocked model '{model}'"
    if is_blocked_base_url(str(base_url) if base_url else None):
        return True, f"blocked base_url '{base_url}'"
    # also check composite strings where model contains custom+loopback hint
    combined = f"{model} {base_url}".lower()
    if is_blocked_model(combined):
        return True, "blocked model substring in composite"
    if is_blocked_base_url(combined):
        return True, "blocked base_url in composite"
    return False, ""


def sanitize_entry(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return entry if allowed, else None.  Normalizes opencode -> opencode-go."""
    if not entry or not isinstance(entry, dict):
        return None
    blocked, reason = is_blocked_entry(entry)
    if blocked:
        try:
            from .env_gate import fail_open_telemetry  # type: ignore

            fail_open_telemetry("model_guard", "blocked_entry_filtered", reason=reason, entry=str(entry)[:200])  # type: ignore[call-arg]
        except Exception:
            logger.warning("[model_guard] blocked_entry_filtered reason=%s entry=%.200s", reason, str(entry))
        return None
    # normalize provider alias
    out = dict(entry)
    if "provider" in out and out["provider"]:
        n = _normalize_provider(str(out["provider"]))
        if n:
            out["provider"] = n
    if not is_allowed_provider(out.get("provider")) and out.get("provider"):
        # provider present but not allowed (e.g. litellm, bridged) — treat as blocked unless explicitly empty
        prov = str(out.get("provider") or "").strip().lower()
        # hermes/default are internal and allowed as pass-through for adapter's own bookkeeping
        if prov not in ("hermes", "safe", "unknown", "default"):
            logger.warning("[model_guard] provider not in allowlist, filtering: %r", prov)
            try:
                from .env_gate import fail_open_telemetry

                fail_open_telemetry("model_guard", "provider_not_allowlisted", provider=prov)
            except Exception:
                pass
            return None
    return out


def sanitize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Strip persisted model/provider overrides from session metadata if blocked.
    Returns sanitized copy.  Never mutates input.  Extra keys not in
    _PERSISTED_OVERRIDE_KEYS are preserved.
    """
    if not metadata:
        return {}
    out: dict[str, Any] = {}
    # check for nested "model_override" dicts or flat provider/model/base_url
    for k, v in dict(metadata).items():
        lk = k.lower() if isinstance(k, str) else k
        # Check if this key is a known override carrier
        if lk in {s.lower() for s in _PERSISTED_OVERRIDE_KEYS} or lk in ("model_override", "fallback_model", "fallback_chain"):
            # value may be dict (entry) or string
            if isinstance(v, dict):
                blocked, reason = is_blocked_entry(v)
                if blocked:
                    logger.warning("[model_guard] stripping blocked metadata[%s]: %s reason=%s", k, v, reason)
                    try:
                        from .env_gate import fail_open_telemetry  # type: ignore

                        fail_open_telemetry("model_guard", "blocked_metadata_filtered", key=k, reason=reason)  # type: ignore[call-arg]
                    except Exception:
                        pass
                    continue  # drop
                # also handle bare provider alias normalization for dict values
                sanitized = sanitize_entry(v)
                if sanitized is None:
                    continue
                out[k] = sanitized
                continue
            if isinstance(v, list):
                # e.g. fallback_chain list of entries
                kept = []
                for item in v:
                    if isinstance(item, dict):
                        s = sanitize_entry(item)
                        if s is not None:
                            kept.append(s)
                        else:
                            logger.warning("[model_guard] filtered list item in metadata[%s]: %r", k, item)
                    elif isinstance(item, str):
                        blocked, reason = is_blocked_entry(item)
                        if blocked:
                            logger.warning("[model_guard] filtered str list item in metadata[%s]: %r reason=%s", k, item, reason)
                            continue
                        kept.append(item)
                    else:
                        kept.append(item)
                out[k] = kept
                continue
            if isinstance(v, str):
                if is_blocked_model(v):
                    logger.warning("[model_guard] stripping blocked metadata[%s]=%r", k, v)
                    continue
                if lk == "provider":
                    n = _normalize_provider(v)
                    if n and not is_allowed_provider(n) and n not in ("hermes", "safe", "default", "unknown"):
                        logger.warning("[model_guard] stripping disallowed provider metadata[%s]=%r", k, v)
                        continue
                    if n:
                        out[k] = n
                        continue
                if is_blocked_base_url(v):
                    logger.warning("[model_guard] stripping blocked metadata base_url[%s]=%r", k, v)
                    continue
                out[k] = v
                continue
            # other types (bool/int) pass through
            out[k] = v
        else:
            # not an override key — check composite: metadata["hermes"] or similar may carry nested dict
            if isinstance(v, dict):
                # shallow check for embedded provider/model hiding inside other keys
                # if the dict itself looks like an entry (has provider/model), sanitize it
                if "provider" in v or "model" in v or "base_url" in v or "baseUrl" in v:
                    blocked, reason = is_blocked_entry(v)
                    if blocked:
                        logger.warning("[model_guard] stripping embedded blocked entry at metadata[%s]: %s reason=%s", k, v, reason)
                        continue
                    sanitized = sanitize_entry(v)
                    if sanitized is not None:
                        out[k] = sanitized
                    continue
            out[k] = v
    # extra safety: also scan PROVIDER/MODEL env-style keys case-insensitive
    return out


def guard_session_record(rec: Any) -> Any:
    """Sanitize SessionRecord.metadata in-place (safe, never raises).
    Returns the same record with blocked overrides stripped.  Logs via fail_open_telemetry.
    """
    try:
        md = getattr(rec, "metadata", None)
        if isinstance(md, dict):
            cleaned = sanitize_metadata(md)
            # only assign if something was removed (avoid needless churn)
            if cleaned.keys() != md.keys() or any(cleaned.get(k) != md.get(k) for k in cleaned):
                # if blocked items existed, lengths differ
                rec.metadata = cleaned
            elif len(cleaned) != len(md):
                rec.metadata = cleaned
            else:
                # same keys but values may have been filtered inside lists/dicts
                rec.metadata = cleaned
    except Exception as e:
        logger.debug("[model_guard] guard_session_record no-op: %s", e)
    return rec
