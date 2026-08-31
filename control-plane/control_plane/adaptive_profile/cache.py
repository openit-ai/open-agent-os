"""Adaptive Profile policy cache — Redis with in-memory fallback.

Key: profile:policy:{tenant_id}:{user_id}:{task_type}:{profile_version}
Value: JSON {"policy": <7-key minimal policy>}
TTL: 600s (best-effort). All ops fail-safe (never raise).
Exports match __init__.py: get_cached_policy, set_cached_policy,
invalidate_user_cache, set_cache_client, clear_cache_client, cache_key_for_test.
"""
from __future__ import annotations
import json
import time
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TTL_SECONDS = 600
_PREFIX = "profile:policy"
_CACHE_PREFIX = "profile:policy"

# Injectable client (redis-py / fakeredis). None => use in-memory fallback or lazy env Redis.
_client: Any | None = None
_override_client: Any | None = None

# In-memory fallback: key -> (json_str, expiry_ts)
_fallback: dict[str, tuple[str, float]] = {}

def cache_key_for_test(tenant_id: str, user_id: str, task_type: str, profile_version: int | str) -> str:
    """Deterministic cache key — exposed for tests."""
    tt = str(task_type or "general_chat")
    ver = str(profile_version)
    # sanitize : and | to avoid injection but keep deterministic
    def _safe(s: str) -> str:
        return s.replace(":", "_").replace("|", "_").replace(" ", "_")
    return f"{_PREFIX}:{_safe(str(tenant_id))}:{_safe(str(user_id))}:{_safe(tt)}:{_safe(ver)}"

def _cache_key(tenant_id: str, user_id: str, task_type: str, profile_version: int | str) -> str:
    return cache_key_for_test(tenant_id, user_id, task_type, profile_version)

def set_cache_client(client: Any) -> None:
    """Inject a Redis-compatible client (used by tests with fakeredis)."""
    global _client, _override_client
    _client = client
    _override_client = client

def clear_cache_client() -> None:
    """Clear injected client and in-memory fallback (test helper)."""
    global _client, _override_client
    _client = None
    _override_client = None
    _fallback.clear()

def _get_client() -> Any | None:
    global _client
    if _client is not None:
        return _client
    # lazy: try REDIS_URL env if redis lib available
    redis_url = os.getenv("REDIS_URL", "") or os.getenv("OAOS_REDIS_URL", "")
    if not redis_url or "://" not in redis_url:
        return None
    try:
        import redis as _redis  # type: ignore
        # Use from_url with short timeouts
        c = _redis.Redis.from_url(redis_url, socket_connect_timeout=0.8, socket_timeout=0.8, decode_responses=False)
        # don't cache this auto client as global to avoid stale; but store for reuse
        _client = c
        return c
    except Exception as e:
        logger.debug(f"cache lazy redis unavailable: {e}")
        return None

def get_cached_policy(tenant_id: str, user_id: str, task_type: str, profile_version: int | str) -> Optional[dict[str, Any]]:
    """Return {'policy': {...}} or None. Never raises."""
    key = _cache_key(tenant_id, user_id, task_type, profile_version)
    # try injected/redis first
    client = _get_client()
    has_injected = _override_client is not None
    has_redis_url = bool((os.getenv("REDIS_URL", "") or os.getenv("OAOS_REDIS_URL", "")).strip())
    if client is not None:
        try:
            raw = client.get(key)
            if raw is None:
                # also check fallback for test compatibility when injected client
                if has_injected:
                    pass
                else:
                    # if redis URL configured but key not found, don't fallback (fail-safe)
                    if has_redis_url:
                        return None
            else:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                # raw may already be str
                data = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(data, dict) and "policy" in data:
                    return data
                # if stored as raw policy dict without wrapper (legacy), wrap
                if isinstance(data, dict):
                    # assume it's policy itself if contains known keys
                    if any(k in data for k in ("verbosity", "conclusion_first")):
                        return {"policy": data}
                    return data
        except Exception as e:
            logger.debug(f"cache get redis failed key={key}: {e}")
            # fail-safe: if redis URL configured, don't fallback, just return None
            if has_redis_url and not has_injected:
                return None
            # fall through to fallback only for injected or no URL case
    # in-memory fallback: only when no redis URL or injected client present
    if has_redis_url and not has_injected:
        return None
    try:
        entry = _fallback.get(key)
        if entry is None:
            return None
        raw_str, exp = entry
        if exp and time.monotonic() > exp:
            _fallback.pop(key, None)
            return None
        data = json.loads(raw_str)
        if isinstance(data, dict) and "policy" in data:
            return data
        if isinstance(data, dict):
            return {"policy": data} if any(k in data for k in ("verbosity", "conclusion_first")) else data
        return None
    except Exception as e:
        logger.debug(f"cache get fallback failed key={key}: {e}")
        return None

def set_cached_policy(tenant_id: str, user_id: str, task_type: str, profile_version: int | str, policy: dict[str, Any]) -> None:
    """Store policy with TTL. Never raises."""
    if not isinstance(policy, dict):
        return
    key = _cache_key(tenant_id, user_id, task_type, profile_version)
    payload = json.dumps({"policy": policy}, ensure_ascii=False)
    client = _get_client()
    has_injected = _override_client is not None
    has_redis_url = bool((os.getenv("REDIS_URL", "") or os.getenv("OAOS_REDIS_URL", "")).strip())
    if client is not None:
        try:
            # redis setex
            # need to handle both decode_responses cases
            try:
                client.set(key, payload, ex=_TTL_SECONDS)
            except TypeError:
                # fakeredis or older: setex
                client.setex(key, _TTL_SECONDS, payload)
            # also populate fallback for immediate in-process reads if injected client
            if has_injected:
                _fallback[key] = (payload, time.monotonic() + _TTL_SECONDS)
            return
        except Exception as e:
            logger.debug(f"cache set redis failed key={key}: {e}")
            # fail-safe: if redis URL configured and not injected, don't fallback
            if has_redis_url and not has_injected:
                return
            # fall through to fallback for injected case
    # fallback only when no redis URL or injected
    if has_redis_url and not has_injected:
        return
    try:
        _fallback[key] = (payload, time.monotonic() + _TTL_SECONDS)
    except Exception as e:
        logger.debug(f"cache set fallback failed: {e}")

def invalidate_user_cache(tenant_id: str, user_id: str) -> int:
    """Delete all cached policies for tenant/user across task_types/versions. Never raises. Returns deleted count."""
    # sanitize same as key prefix
    def _safe(s: str) -> str:
        return s.replace(":", "_").replace("|", "_").replace(" ", "_")
    prefix = f"{_PREFIX}:{_safe(str(tenant_id))}:{_safe(str(user_id))}:"
    deleted = 0
    client = _get_client()
    if client is not None:
        try:
            # scan keys
            keys: list[Any] = []
            try:
                # redis-py scan_iter
                if hasattr(client, "scan_iter"):
                    for k in client.scan_iter(match=prefix + "*"):
                        keys.append(k)
                elif hasattr(client, "keys"):
                    k2 = client.keys(prefix + "*")
                    if isinstance(k2, (list, tuple, set)):
                        keys.extend(k2)
                    elif k2:
                        keys.append(k2)
            except Exception as se:
                logger.debug(f"cache scan failed: {se}")
            # decode bytes keys if needed
            decoded_keys: list[str] = []
            for k in keys:
                if isinstance(k, bytes):
                    try:
                        decoded_keys.append(k.decode())
                    except Exception:
                        decoded_keys.append(str(k))
                else:
                    decoded_keys.append(str(k))
            if decoded_keys:
                try:
                    res = client.delete(*decoded_keys)
                    if isinstance(res, int):
                        deleted += res
                    else:
                        deleted += len(decoded_keys)
                except Exception:
                    for kk in decoded_keys:
                        try:
                            r = client.delete(kk)
                            if r:
                                deleted += 1 if isinstance(r, int) else 1
                            else:
                                deleted += 0
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"cache invalidate redis failed: {e}")
    # always clear fallback matching prefix
    try:
        to_del = [k for k in list(_fallback.keys()) if k.startswith(prefix)]
        deleted += len(to_del)
        for k in to_del:
            _fallback.pop(k, None)
    except Exception as e:
        logger.debug(f"cache invalidate fallback failed: {e}")
    return deleted
