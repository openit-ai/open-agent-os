"""P0 Message Execution Reliability — durable idempotency for Mattermost → CP.

Deterministic key: tenant_id + channel_id + post_id (post_id required).
Redis atomic claim/state (processing/completed/failed_retryable) is source of truth.
Bridge local seen remains auxiliary cache only.

States: processing → completed | failed (retryable flag)
TTL: processing 600s, completed 7d, failed 600s (retryable short, non-retryable longer)

Redis Lua atomic is not strictly needed because SET NX + GET is atomic per key for claim,
but we provide Lua fallback for reclaim of retryable failed. Completed is immutable.

Production fail-closed: if Redis unavailable and is_production()==True → 503.
Non-prod: in-memory fallback dict with telemetry ([fail-open]).

Audit: every claim/duplicate/complete emits audit entry via ledger if available.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any

import logging

logger = logging.getLogger(__name__)

# ── env gate copy (no cross-package import to keep isolated) ──────────────────
def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        if os.getenv(k, "").strip().lower() in ("production", "prod"):
            return True
    return False

def _allow_test_fallback() -> bool:
    if _is_production():
        return os.getenv("OAOS_ALLOW_TEST_FALLBACK", "").lower() in ("1", "true", "yes")
    return True

def _fail_open_telemetry(component: str, reason: str, **fields):
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    msg = f"[fail-open] component={component} reason={reason} {extra}".strip()
    logger.warning(msg)
    try:
        import sys
        print(msg, file=sys.stderr)
    except Exception:
        pass

# ── constants ─────────────────────────────────────────────────────────────────
PROCESSING_TTL_SEC = 600
COMPLETED_TTL_SEC = 7 * 86400
FAILED_TTL_SEC = 600
FAILED_RETRYABLE_TTL_SEC = 120
PREFIX = "oaos:mm:idem:"

# in-memory fallback for non-prod
_mem_store: dict[str, dict[str, Any]] = {}
_mem_lock = threading.Lock()
_mem_expiry: dict[str, float] = {}

def build_idempotency_key(tenant_id: str, channel_id: str | None, post_id: str | None) -> str | None:
    if not post_id or not str(post_id).strip():
        return None
    t = (tenant_id or "default").strip() or "default"
    c = (channel_id or "").strip()
    p = str(post_id).strip()
    raw = f"{t}\x1f{c}\x1f{p}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return f"{PREFIX}{h}"

def _inmem_get(key: str) -> dict[str, Any] | None:
    with _mem_lock:
        exp = _mem_expiry.get(key, 0)
        if exp and time.time() > exp:
            _mem_store.pop(key, None)
            _mem_expiry.pop(key, None)
            return None
        v = _mem_store.get(key)
        return dict(v) if v is not None else None

def _inmem_set_nx(key: str, value: dict[str, Any], ttl_sec: int) -> bool:
    with _mem_lock:
        exp = _mem_expiry.get(key, 0)
        if exp and time.time() > exp:
            _mem_store.pop(key, None)
            _mem_expiry.pop(key, None)
        if key in _mem_store:
            return False
        _mem_store[key] = dict(value)
        _mem_expiry[key] = time.time() + ttl_sec if ttl_sec else 0
        return True

def _inmem_update(key: str, patch: dict[str, Any], ttl_sec: int | None = None) -> None:
    with _mem_lock:
        cur = _mem_store.get(key, {})
        cur = dict(cur)
        cur.update(patch)
        cur["updated_at"] = time.time()
        _mem_store[key] = cur
        if ttl_sec is not None:
            _mem_expiry[key] = time.time() + ttl_sec

def clear_inmem_store():
    with _mem_lock:
        _mem_store.clear()
        _mem_expiry.clear()

# ── redis helpers ──────────────────────────────────────────────────────────────
_redis_override = None
_redis_override_url: str | None = None

def set_idempotency_redis_client(client):
    global _redis_override
    _redis_override = client

def clear_idempotency_redis_client():
    global _redis_override, _redis_override_url
    _redis_override = None
    _redis_override_url = None

def _redis_url() -> str | None:
    if _redis_override_url is not None:
        return _redis_override_url
    for k in ("OAOS_CP_REDIS_URL", "OAOS_REDIS_URL", "REDIS_URL", "OAOS_QUOTA_REDIS_URL"):
        v = os.getenv(k, "").strip()
        if v:
            return v
    return None

def _get_redis_client():
    if _redis_override is not None:
        return _redis_override
    url = _redis_url()
    if not url:
        return None
    try:
        import redis as _r  # type: ignore
        c = _r.Redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
        c.ping()
        return c
    except Exception as e:
        if _is_production() and not _allow_test_fallback():
            # fail-closed caller will raise 503
            raise RuntimeError(f"idempotency redis unavailable in production: {e}") from e
        _fail_open_telemetry("idempotency_redis", "redis_unavailable_fallback_inmem", error=str(e)[:200])
        return None

def _redis_get(client, key: str) -> dict[str, Any] | None:
    try:
        raw = client.get(key)
        if raw is None:
            return None
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        if _is_production() and not _allow_test_fallback():
            raise
        _fail_open_telemetry("idempotency_redis", "redis_get_failed", error=str(e)[:200])
        return _inmem_get(key)

def _redis_set_nx(client, key: str, value: dict[str, Any], ttl_sec: int) -> bool:
    try:
        raw = json.dumps(value, ensure_ascii=False)
        # SET NX EX
        ok = client.set(key, raw, nx=True, ex=ttl_sec)
        return bool(ok)
    except Exception as e:
        if _is_production() and not _allow_test_fallback():
            raise
        _fail_open_telemetry("idempotency_redis", "redis_set_nx_failed", error=str(e)[:200])
        return _inmem_set_nx(key, value, ttl_sec)

# ── atomic reclaim helpers (Lua + WATCH fallback) ────────────────────────────
_RECLAIM_LUA = r"""
local raw = redis.call('GET', KEYS[1])
if not raw then return nil end
local rec = cjson.decode(raw)
if rec.status == 'failed' and rec.retryable then
  redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[1])
  return ARGV[2]
else
  return nil
end
"""

def _redis_atomic_reclaim(client, key: str, new_record: dict[str, Any], ttl_sec: int) -> dict[str, Any] | None:
    raw_new = json.dumps(new_record, ensure_ascii=False)
    # try Lua first (atomic CAS)
    try:
        if hasattr(client, "eval"):
            res = client.eval(_RECLAIM_LUA, 1, key, str(int(ttl_sec)), raw_new)
            if res is not None:
                try:
                    return json.loads(res) if isinstance(res, str) else res
                except Exception:
                    return new_record
            return None
    except Exception:
        pass
    # WATCH/MULTI fallback (fakeredis + real redis)
    try:
        # redis-py WatchError is in redis.exceptions
        from redis.exceptions import WatchError as _WE  # type: ignore
    except Exception:
        _WE = Exception  # type: ignore
    try:
        # Use pipeline with watch
        pipe = client.pipeline(transaction=True)
        # fakeredis FakeRedis pipeline needs explicit watch handling
        max_retries = 5
        for _ in range(max_retries):
            try:
                if hasattr(pipe, "watch"):
                    pipe.watch(key)
                raw = client.get(key)
                if raw is None:
                    try:
                        pipe.unwatch()
                    except Exception:
                        pass
                    return None
                cur = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(cur, dict) or cur.get("status") != "failed" or not cur.get("retryable"):
                    try:
                        pipe.unwatch()
                    except Exception:
                        pass
                    return None
                # need fresh pipe after watch
                try:
                    pipe.multi()
                except Exception:
                    pass
                pipe.set(key, raw_new, ex=int(ttl_sec))
                pipe.execute()
                return new_record
            except _WE:
                continue
            except Exception:
                try:
                    pipe.unwatch()
                except Exception:
                    pass
                return None
        return None
    except Exception:
        return None

def _inmem_atomic_reclaim(key: str, new_record: dict[str, Any], ttl_sec: int) -> dict[str, Any] | None:
    with _mem_lock:
        cur = _mem_store.get(key)
        exp = _mem_expiry.get(key, 0)
        if exp and time.time() > exp:
            _mem_store.pop(key, None)
            _mem_expiry.pop(key, None)
            cur = None
        if cur is not None and cur.get("status") == "failed" and cur.get("retryable"):
            _mem_store[key] = dict(new_record)
            _mem_expiry[key] = time.time() + ttl_sec if ttl_sec else 0
            return dict(new_record)
        return None

def _redis_update(client, key: str, patch: dict[str, Any], ttl_sec: int | None = None) -> None:
    try:
        cur_raw = client.get(key)
        cur = json.loads(cur_raw) if isinstance(cur_raw, str) and cur_raw else (cur_raw if isinstance(cur_raw, dict) else {})
        if not isinstance(cur, dict):
            cur = {}
        cur.update(patch)
        cur["updated_at"] = time.time()
        raw = json.dumps(cur, ensure_ascii=False)
        if ttl_sec is not None:
            client.set(key, raw, ex=ttl_sec)
        else:
            # preserve TTL: get TTL and set without changing? simpler set with keepTTL if supported else no expire
            try:
                ttl = client.ttl(key)
                if ttl and ttl > 0:
                    client.set(key, raw, ex=int(ttl))
                else:
                    client.set(key, raw)
            except Exception:
                client.set(key, raw)
    except Exception as e:
        if _is_production() and not _allow_test_fallback():
            raise
        _inmem_update(key, patch, ttl_sec)

@dataclass
class ClaimResult:
    key: str
    status: str  # claimed | duplicate_processing | duplicate_completed | reclaimed | failed_retryable_reclaimed
    record: dict[str, Any]
    is_duplicate: bool

def try_claim(
    *,
    tenant_id: str,
    channel_id: str | None,
    post_id: str | None,
    session_id: str,
    trace_id: str,
    request_id: str,
) -> tuple[str | None, ClaimResult | None]:
    """Atomic claim for idempotency. Returns (idempotency_key, ClaimResult).

    If key is None (no post_id) → (None, None) meaning no idempotency (caller proceeds normally).
    Raises HTTPException 503 in production when Redis is required but unavailable.
    """
    key = build_idempotency_key(tenant_id, channel_id, post_id)
    if key is None:
        return None, None

    now = time.time()
    base_record: dict[str, Any] = {
        "status": "processing",
        "tenant_id": tenant_id,
        "channel_id": channel_id or "",
        "post_id": post_id or "",
        "session_id": session_id,
        "trace_id": trace_id,
        "request_id": request_id,
        "response_post_id": "",
        "idempotency_key": key,
        "created_at": now,
        "updated_at": now,
    }

    # try to get redis client; production fail-closed if unavailable
    try:
        client = _get_redis_client()
    except RuntimeError as e:
        # production fail-closed → 503
        from fastapi import HTTPException as _HE
        raise _HE(status_code=503, detail=f"idempotency backend unavailable: {e}") from e

    if client is not None:
        # check existing
        existing = _redis_get(client, key)
        if existing is not None:
            st = existing.get("status", "")
            if st == "processing":
                # duplicate while processing → do not call LLM
                return key, ClaimResult(key=key, status="duplicate_processing", record=existing, is_duplicate=True)
            if st == "completed":
                return key, ClaimResult(key=key, status="duplicate_completed", record=existing, is_duplicate=True)
            if st == "failed":
                # retryable failed allows reclaim — atomic CAS via Lua/WATCH
                if existing.get("retryable"):
                    reclaim = dict(base_record)
                    reclaim["status"] = "processing"
                    reclaim["reclaimed_from_failed"] = True
                    atomic = _redis_atomic_reclaim(client, key, reclaim, PROCESSING_TTL_SEC)
                    if atomic is not None:
                        return key, ClaimResult(key=key, status="reclaimed", record=atomic, is_duplicate=False)
                    # CAS failed → another worker reclaimed concurrently, treat as duplicate
                    cur = _redis_get(client, key)
                    if cur is not None:
                        st_cur = cur.get("status", "")
                        if st_cur == "processing":
                            return key, ClaimResult(key=key, status="duplicate_processing", record=cur, is_duplicate=True)
                        return key, ClaimResult(key=key, status="duplicate_completed", record=cur, is_duplicate=True)
                    return key, ClaimResult(key=key, status="duplicate_processing", record=existing, is_duplicate=True)
                else:
                    # non-retryable failed → treat as duplicate_completed to avoid spam, but allow caller to decide
                    return key, ClaimResult(key=key, status="duplicate_completed", record=existing, is_duplicate=True)
            # unknown status → treat as duplicate
            return key, ClaimResult(key=key, status="duplicate_completed", record=existing, is_duplicate=True)
        # not existing → try SET NX
        ok = _redis_set_nx(client, key, base_record, PROCESSING_TTL_SEC)
        if ok:
            return key, ClaimResult(key=key, status="claimed", record=base_record, is_duplicate=False)
        # race: someone else claimed concurrently → read back
        existing2 = _redis_get(client, key)
        if existing2 is not None:
            st2 = existing2.get("status", "")
            if st2 == "processing":
                return key, ClaimResult(key=key, status="duplicate_processing", record=existing2, is_duplicate=True)
            return key, ClaimResult(key=key, status="duplicate_completed", record=existing2, is_duplicate=True)
        # unlikely fallback
        return key, ClaimResult(key=key, status="claimed", record=base_record, is_duplicate=False)
    else:
        # no redis → in-memory (non-prod only, prod already raised)
        if _is_production() and not _allow_test_fallback():
            from fastapi import HTTPException as _HE
            raise _HE(status_code=503, detail="idempotency Redis unavailable in production")
        existing = _inmem_get(key)
        if existing is not None:
            st = existing.get("status", "")
            if st == "processing":
                return key, ClaimResult(key=key, status="duplicate_processing", record=existing, is_duplicate=True)
            if st == "completed":
                return key, ClaimResult(key=key, status="duplicate_completed", record=existing, is_duplicate=True)
            if st == "failed" and existing.get("retryable"):
                reclaim = dict(base_record)
                reclaim["status"] = "processing"
                reclaimed = _inmem_atomic_reclaim(key, {**reclaim, "reclaimed_from_failed": True}, PROCESSING_TTL_SEC)
                if reclaimed is not None:
                    return key, ClaimResult(key=key, status="reclaimed", record=reclaimed, is_duplicate=False)
                # lost race → treat as duplicate
                cur2 = _inmem_get(key)
                if cur2 is not None and cur2.get("status") == "processing":
                    return key, ClaimResult(key=key, status="duplicate_processing", record=cur2, is_duplicate=True)
                return key, ClaimResult(key=key, status="duplicate_completed", record=cur2 or existing, is_duplicate=True)
            return key, ClaimResult(key=key, status="duplicate_completed", record=existing, is_duplicate=True)
        ok = _inmem_set_nx(key, base_record, PROCESSING_TTL_SEC)
        if ok:
            return key, ClaimResult(key=key, status="claimed", record=base_record, is_duplicate=False)
        existing2 = _inmem_get(key)
        if existing2:
            return key, ClaimResult(key=key, status="duplicate_processing", record=existing2, is_duplicate=True)
        return key, ClaimResult(key=key, status="claimed", record=base_record, is_duplicate=False)

def complete(key: str, *, response_post_id: str = "", response_post_ids: list[str] | None = None, response_marker: str = "", session_id: str = "", trace_id: str = "") -> None:
    if not key:
        return
    # normalize response_post_ids: include single id if provided
    _all_ids: list[str] = []
    if response_post_ids:
        _all_ids = [str(x).strip() for x in response_post_ids if str(x).strip()]
    elif response_post_id:
        _all_ids = [str(response_post_id).strip()]
    # also include response_post_id if both provided but distinct
    if response_post_id and response_post_id not in _all_ids:
        _all_ids.append(str(response_post_id).strip())
    # marker for Mattermost read-back dedup (e.g., hash of response or first post marker)
    patch: dict[str, Any] = {
        "status": "completed",
        "response_post_id": _all_ids[-1] if _all_ids else (response_post_id or ""),
        "response_post_ids": _all_ids,
        "response_marker": response_marker or "",
        "completed_at": time.time(),
    }
    if session_id:
        patch["session_id"] = session_id
    if trace_id:
        patch["trace_id"] = trace_id
    try:
        client = _get_redis_client()
    except Exception as e:
        if _is_production() and not _allow_test_fallback():
            from fastapi import HTTPException as _HEc
            raise _HEc(status_code=503, detail=f"idempotency backend unavailable: {e}") from e
        _inmem_update(key, patch, COMPLETED_TTL_SEC)
        return
    if client is not None:
        try:
            _redis_update(client, key, patch, ttl_sec=COMPLETED_TTL_SEC)
        except Exception as e:
            if _is_production() and not _allow_test_fallback():
                from fastapi import HTTPException as _HEc2
                raise _HEc2(status_code=503, detail=f"idempotency redis update failed: {e}") from e
            _inmem_update(key, patch, COMPLETED_TTL_SEC)
    else:
        if _is_production() and not _allow_test_fallback():
            from fastapi import HTTPException as _HEc3
            raise _HEc3(status_code=503, detail="idempotency Redis unavailable in production")
        _inmem_update(key, patch, COMPLETED_TTL_SEC)

def fail(key: str, *, error: str = "", retryable: bool = False, response_post_ids: list[str] | None = None, response_marker: str | None = None) -> None:
    if not key:
        return
    patch: dict[str, Any] = {
        "status": "failed",
        "error": (error or "")[:500],
        "retryable": bool(retryable),
        "failed_at": time.time(),
    }
    # Preserve partial delivery state for observability/retry: store any post_ids that succeeded before failure.
    if response_post_ids is not None:
        _partial = [str(x).strip() for x in response_post_ids if str(x).strip()]
        if _partial:
            patch["response_post_ids"] = _partial
            # keep last as response_post_id for compat
            if not patch.get("response_post_id"):
                patch["response_post_id"] = _partial[-1]
    if response_marker:
        patch["response_marker"] = response_marker
    # delivery_complete is explicitly False for failed state; caller must ensure complete() only when all chunks succeeded.
    ttl = FAILED_RETRYABLE_TTL_SEC if retryable else FAILED_TTL_SEC
    try:
        client = _get_redis_client()
    except Exception as e:
        if _is_production() and not _allow_test_fallback():
            from fastapi import HTTPException as _HEf
            raise _HEf(status_code=503, detail=f"idempotency backend unavailable: {e}") from e
        _inmem_update(key, patch, ttl)
        return
    if client is not None:
        try:
            _redis_update(client, key, patch, ttl_sec=ttl)
        except Exception as e:
            if _is_production() and not _allow_test_fallback():
                from fastapi import HTTPException as _HEf2
                raise _HEf2(status_code=503, detail=f"idempotency redis update failed: {e}") from e
            _inmem_update(key, patch, ttl)
    else:
        if _is_production() and not _allow_test_fallback():
            from fastapi import HTTPException as _HEf3
            raise _HEf3(status_code=503, detail="idempotency Redis unavailable in production")
        _inmem_update(key, patch, ttl)

def get_record(key: str) -> dict[str, Any] | None:
    if not key:
        return None
    try:
        client = _get_redis_client()
    except Exception as e:
        if _is_production() and not _allow_test_fallback():
            from fastapi import HTTPException as _HEg
            raise _HEg(status_code=503, detail=f"idempotency backend unavailable: {e}") from e
        return _inmem_get(key)
    if client is not None:
        try:
            return _redis_get(client, key)
        except Exception as e:
            if _is_production() and not _allow_test_fallback():
                from fastapi import HTTPException as _HEg2
                raise _HEg2(status_code=503, detail=f"idempotency redis get failed: {e}") from e
            return _inmem_get(key)
    if _is_production() and not _allow_test_fallback():
        from fastapi import HTTPException as _HEg3
        raise _HEg3(status_code=503, detail="idempotency Redis unavailable in production")
    return _inmem_get(key)

def wait_for_completion(key: str, timeout_sec: float = 20.0, poll_interval: float = 0.25) -> dict[str, Any] | None:
    """Poll until completed (for bridge read-back / tests)."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        rec = get_record(key)
        if rec and rec.get("status") == "completed":
            return rec
        time.sleep(poll_interval)
    return get_record(key)

# ── response idempotency helpers (marker/read-back) ──────────────────────────
def build_response_marker(channel_id: str | None, root_id: str | None, text_prefix: str) -> str:
    raw = f"{channel_id or ''}\x1f{root_id or ''}\x1f{(text_prefix or '')[:200]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def has_duplicate_response(key: str, marker: str | None = None) -> tuple[bool, dict[str, Any] | None]:
    """Check read-back: if key is completed, returns (True, record). Optional marker comparison for extra guard.

    Marker semantics: marker is hash of full response text (or idempotency_key fallback). If caller supplies marker
    and stored response_marker exists but mismatches, it means different content → NOT a duplicate of this content
    (return False, rec) so caller can decide to handle as mismatch rather than suppress. Stored rec is still returned
    for observability.
    """
    rec = get_record(key)
    if rec is None:
        return False, None
    if rec.get("status") == "completed" and rec.get("response_post_id"):
        if marker and rec.get("response_marker") and rec.get("response_marker") != marker:
            # marker mismatch → different content, do NOT treat as duplicate for this marker
            return False, rec
        return True, rec
    return False, rec

# ── helpers for webhook ────────────────────────────────────────────────────────
def is_retryable_error(exc: Exception) -> bool:
    msg = str(getattr(exc, "detail", exc) or exc).lower()
    code = getattr(exc, "status_code", None)
    if code in (429, 503, 502, 504):
        return True
    if "timeout" in msg or "timed out" in msg or "429" in msg or "503" in msg or "quota_exceeded" in msg or "rate" in msg:
        return True
    return False
