"""LLM Runtime — wires Session + Streaming + MCP Client per §16C.

Minimal built-in runtime: session lifecycle, token streaming, MCP tool loop.
No hard dependency on litellm — if litellm is installed and LLM_API_KEY is set,
it will be used; otherwise a mock streaming response is emitted so tests/offline
still pass.  No shell/python execution (LLM-only, §16F.1).

Enhanced with pydantic-ai inspired patterns (clean-room, BSL):
  1) OAOSContext (tenant_id, agent_id, trace_id, vault_path, policy) injected into every tool
  2) output_type: BaseModel support with validation and retry (max 2)
  3) ToolOutputLimits (truncate 4000, JSON schema check, auto retry)

Usage:
    from agent_runtime.llm_runtime import LLMRuntime, OAOSContext, ToolOutputLimits
    rt = LLMRuntime()
    sess = rt.create_session(tenant_id="t", agent_id="a", user_id="u")
    ctx = OAOSContext(tenant_id="t", agent_id="a", trace_id=sess["trace_id"])
    async for ev in rt.stream_prompt(sess["session_id"], tenant_id="t", agent_id="a", prompt="hi"):
        print(ev)

Provider-level:
    from agent_runtime.llm_runtime import LLMProviderAdapter
    adapter = LLMProviderAdapter(model="gpt-4o-mini")
    result = await adapter.completion(messages, output_type=MyModel)  # validates + retries
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, AsyncGenerator, Callable, Awaitable, get_origin, get_args

logger = logging.getLogger(__name__)

from pydantic import BaseModel, ValidationError

from .session import SessionManager, OAOSContext  # re-export
from .streaming import StreamingEngine
from .mcp_client import MCPClient

# Re-export OAOSContext for external callers
__all__ = [
    "OAOSContext",
    "ToolOutputLimits",
    "ModelRouting",
    "LLMProviderAdapter",
    "StructuredToolLoop",
    "LLMRuntime",
    "LLMRuntimeAdapter",
    "default_runtime",
    "AuditEvent",
    "AuditLogStub",
    "default_audit_log",
    "ProviderType",
    "RuntimeMode",
]

# ---------------------------------------------------------------------------
# ProviderType + RuntimeMode — multi-provider adapter
# ---------------------------------------------------------------------------
from enum import Enum

class ProviderType(str, Enum):
    """LLM provider type — 6 providers (Argo runners style)."""
    CLAUDE = "claude"
    CODEX = "codex"
    GEMINI = "gemini"
    OPENCODE_GO = "opencode-go"
    OPENROUTER = "openrouter"
    OLLAMA = "ollama"
    # backward compat: opencode -> opencode-go
    OPENCODE = "opencode"

    @classmethod
    def from_str(cls, v: str | None) -> "ProviderType | None":
        if not v:
            return None
        s = v.lower().strip()
        if s == "opencode":
            s = "opencode-go"
        try:
            return cls(s)
        except ValueError:
            return None

class RuntimeMode(str, Enum):
    """Runtime mode: llm (direct provider) vs hermes (delegate to Hermes Agent)."""
    LLM = "llm"
    HERMES = "hermes"

    @classmethod
    def from_str(cls, v: str | None) -> "RuntimeMode":
        if not v:
            return cls.LLM
        s = v.lower().strip()
        if s in ("hermes", "hermes_agent", "agent"):
            return cls.HERMES
        return cls.LLM

def _resolve_runtime_mode(explicit: str | RuntimeMode | None = None) -> RuntimeMode:
    """Resolve runtime mode: explicit > env OAOS_RUNTIME_MODE > default llm.
    Also attempts to fetch from admin-console API if ADMIN_CONSOLE_URL set.
    """
    if isinstance(explicit, RuntimeMode):
        return explicit
    if isinstance(explicit, str) and explicit:
        m = RuntimeMode.from_str(explicit)
        if m == RuntimeMode.HERMES:
            return m
        # if explicit string is valid llm mode, respect it
        if explicit.lower().strip() in ("llm", "direct"):
            return RuntimeMode.LLM
    # env
    env_val = os.getenv("OAOS_RUNTIME_MODE") or os.getenv("RUNTIME_MODE") or os.getenv("OAOS_AGENT_RUNTIME_MODE") or ""
    if env_val:
        return RuntimeMode.from_str(env_val)
    # Try admin-console API sync fetch (best-effort, non-blocking, 1.5s timeout)
    # Only attempt if ADMIN_CONSOLE_URL is set to avoid slow path
    admin_base = os.getenv("ADMIN_CONSOLE_URL") or os.getenv("OAOS_ADMIN_CONSOLE_URL") or os.getenv("OAOS_ADMIN_API_URL") or ""
    if admin_base:
        try:
            import httpx  # type: ignore
            # try several plausible endpoints
            for endpoint in ("/v1/config/runtime", "/v1/llm/config", "/v1/system/config"):
                try:
                    url = admin_base.rstrip("/") + endpoint
                    import httpx as _hx
                    with _hx.Client(timeout=1.5) as c:
                        r = c.get(url)
                        if r.status_code == 200:
                            data = r.json()
                            # accept {"runtime_mode": "hermes"} or {"mode": ...}
                            raw = data.get("runtime_mode") or data.get("mode") or data.get("runtimeMode") or ""
                            if raw:
                                m = RuntimeMode.from_str(str(raw))
                                if m:
                                    return m
                except Exception:
                    continue
        except Exception:
            pass
    return RuntimeMode.LLM

def _resolve_provider_from_env() -> ProviderType | None:
    raw = os.getenv("OAOS_LLM_PROVIDER") or os.getenv("LLM_PROVIDER") or os.getenv("OAOS_PROVIDER") or os.getenv("PROVIDER_TYPE") or ""
    if raw:
        return ProviderType.from_str(raw)
    return None

def _admin_console_base_url() -> str | None:
    return (os.getenv("ADMIN_CONSOLE_URL") or os.getenv("OAOS_ADMIN_CONSOLE_URL") or os.getenv("OAOS_ADMIN_API_URL") or "").rstrip("/") or None

def _fetch_provider_config_from_admin_api(provider: str | None = None) -> dict[str, Any] | None:
    """Best-effort fetch provider config from admin-console API (sync, short timeout).
    Endpoints attempted: /v1/llm/config, /v1/llm/provider-config, /v1/config/llm
    Returns dict with keys like {provider, model, api_key, base_url} or None.
    """
    base = _admin_console_base_url()
    if not base:
        return None
    try:
        import httpx as _hx
        # candidate endpoints
        candidates = ["/v1/llm/config", "/v1/llm/provider-config", "/v1/config/llm", "/v1/providers/config"]
        for ep in candidates:
            try:
                url = base + ep
                params: dict[str, str] = {}
                if provider:
                    params["provider"] = provider
                with _hx.Client(timeout=1.5) as c:
                    # pass admin token if available
                    headers: dict[str, str] = {}
                    tok = os.getenv("ADMIN_API_TOKEN") or os.getenv("OAOS_ADMIN_TOKEN") or ""
                    if tok:
                        headers["Authorization"] = f"Bearer {tok}"
                    r = c.get(url, params=params or None, headers=headers or None)
                    if r.status_code == 200:
                        data = r.json()
                        # Normalize: may be {config: {...}} or direct
                        if isinstance(data, dict):
                            if "config" in data and isinstance(data["config"], dict):
                                return data["config"]
                            # if response contains provider key, return as-is
                            if any(k in data for k in ("provider", "model", "api_key", "base_url", "provider_type")):
                                return data
                            # if keyed by provider name
                            if provider and provider in data and isinstance(data[provider], dict):
                                return data[provider]
                        return data if isinstance(data, dict) else None
            except Exception:
                continue
    except Exception:
        pass
    return None

def _provider_env_config(provider: ProviderType | str | None) -> dict[str, Any]:
    """Collect provider config from env vars (no network)."""
    if isinstance(provider, ProviderType):
        key = provider.value.lower()
    elif isinstance(provider, str) and provider:
        # Handle 'ProviderType.OLLAMA' string repr fallback
        key = provider.lower().split(".")[-1]
    else:
        key = ""
    out: dict[str, Any] = {}
    if key in ("claude", ""):
        out["claude_api_key"] = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY") or os.getenv("OAOS_CLAUDE_API_KEY") or ""
        out["claude_base_url"] = os.getenv("ANTHROPIC_BASE_URL") or os.getenv("CLAUDE_BASE_URL") or ""
        out["claude_model"] = os.getenv("CLAUDE_MODEL") or ""
    if key in ("codex", ""):
        out["codex_api_key"] = os.getenv("OPENAI_API_KEY") or os.getenv("CODEX_API_KEY") or os.getenv("OAOS_CODEX_API_KEY") or ""
        out["codex_base_url"] = os.getenv("OPENAI_BASE_URL") or os.getenv("CODEX_BASE_URL") or os.getenv("OAOS_CODEX_BASE_URL") or ""
        out["codex_model"] = os.getenv("CODEX_MODEL") or ""
    if key in ("gemini", ""):
        out["gemini_api_key"] = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_GENAI_API_KEY") or os.getenv("OAOS_GEMINI_API_KEY") or ""
        out["gemini_model"] = os.getenv("GEMINI_MODEL") or os.getenv("GOOGLE_GEMINI_MODEL") or ""
    if key in ("opencode-go", "opencode", ""):
        out["opencode_go_api_key"] = os.getenv("OPENCODE_API_KEY") or os.getenv("OAOS_OPENCODE_API_KEY") or ""
        out["opencode_go_base_url"] = os.getenv("OPENCODE_API_URL") or os.getenv("OPENCODE_BASE_URL") or os.getenv("OAOS_OPENCODE_BASE_URL") or "http://localhost:4096"
        out["opencode_go_model"] = os.getenv("OPENCODE_MODEL") or os.getenv("OAOS_OPENCODE_GO_MODEL") or ""
        # legacy keys for compat
        out["opencode_api_key"] = out["opencode_go_api_key"]
        out["opencode_base_url"] = out["opencode_go_base_url"]
        out["opencode_model"] = out["opencode_go_model"]
    if key in ("openrouter", ""):
        out["openrouter_api_key"] = os.getenv("OPENROUTER_API_KEY") or os.getenv("OAOS_OPENROUTER_API_KEY") or ""
        out["openrouter_base_url"] = os.getenv("OPENROUTER_BASE_URL") or os.getenv("OAOS_OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
        out["openrouter_model"] = os.getenv("OPENROUTER_MODEL") or os.getenv("OAOS_OPENROUTER_MODEL") or "openrouter/auto"
    if key in ("ollama", ""):
        out["ollama_base_url"] = os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST") or os.getenv("OAOS_OLLAMA_BASE_URL") or "http://localhost:11434"
        out["ollama_model"] = os.getenv("OLLAMA_MODEL") or os.getenv("OAOS_OLLAMA_MODEL") or "llama3"
    # generic
    out["generic_model"] = os.getenv("OAOS_LLM_MODEL") or os.getenv("LLM_MODEL") or ""
    return out

def _resolve_provider_config(provider: ProviderType | str | None, *, prefer_admin_api: bool = True) -> dict[str, Any]:
    """Resolve provider config: admin-console API (if available) merged over env."""
    env_cfg = _provider_env_config(provider)
    if not prefer_admin_api:
        return env_cfg
    api_cfg = _fetch_provider_config_from_admin_api(provider.value.lower() if isinstance(provider, ProviderType) else (str(provider).lower().split(".")[-1] if provider else None))
    if not api_cfg:
        return env_cfg
    # merge: api overrides env where non-empty
    merged = dict(env_cfg)
    for k, v in api_cfg.items():
        if v is not None and v != "":
            merged[k] = v
            # also map generic keys
            if k in ("api_key", "base_url", "model") and provider:
                pkey = provider.value.lower() if isinstance(provider, ProviderType) else str(provider).lower().split(".")[-1]
                merged[f"{pkey}_{k}"] = v
                merged[k] = v
    # also map provider_type/model generic
    if "provider_type" in api_cfg:
        merged["provider_type"] = api_cfg["provider_type"]
    if "provider" in api_cfg:
        merged["provider"] = api_cfg["provider"]
    return merged


# ---------------------------------------------------------------------------
# 1) OAOSContext — already defined in session.py, re-exported
# Traceable context injected into every tool call.
# ---------------------------------------------------------------------------
# (OAOSContext imported above)

def _ensure_context(
    ctx: OAOSContext | dict[str, Any] | None,
    session: dict[str, Any] | Any | None = None,
    trace_id: str = "",
    policy: Any | None = None,
) -> OAOSContext:
    if isinstance(ctx, OAOSContext):
        return ctx
    if isinstance(ctx, dict):
        # dict shaped like OAOSContext
        return OAOSContext(
            tenant_id=str(ctx.get("tenant_id", "")),
            agent_id=str(ctx.get("agent_id", "")),
            trace_id=str(ctx.get("trace_id", "") or trace_id),
            vault_path=str(ctx.get("vault_path", "") or ""),
            policy=ctx.get("policy", policy),
            session_id=str(ctx.get("session_id", "")),
            user_id=str(ctx.get("user_id", "")),
            request_id=str(ctx.get("request_id", "")),
        )
    if session is not None:
        return OAOSContext.from_session(session, trace_id=trace_id, policy=policy)
    # minimal fallback
    return OAOSContext(trace_id=trace_id or f"trace_{uuid.uuid4().hex[:8]}", policy=policy)


def _inject_oaos_context(fn: Callable[..., Any], ctx: OAOSContext, args: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Inspect fn signature; if it expects OAOSContext injection, prepend ctx.

    Injection triggers when first parameter is named ctx/context/oaos_context/deps
    or annotated as OAOSContext. Returns (positional_prefix, remaining_kwargs).
    Clean-room: custom introspection, not MIT.
    """
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        if not params:
            return (), args
        first = params[0]
        name = first.name.lower()
        ann = first.annotation
        expects_ctx = False
        # name heuristic
        if name in ("ctx", "context", "oaos_context", "oaosctx", "deps", "deps_type"):
            expects_ctx = True
        # annotation heuristic
        try:
            # direct type check or string annotation
            if ann is OAOSContext:
                expects_ctx = True
            elif isinstance(ann, str) and "OAOSContext" in ann:
                expects_ctx = True
            elif get_origin(ann) is not None:
                # Union etc not needed
                pass
        except Exception:
            pass
        if expects_ctx:
            # inject as first positional arg, keep args as kwargs
            return (ctx,), args
        # also check if any param named oaos_context exists as kw
        for p in params:
            if p.name.lower() in ("oaos_context", "ctx", "context") and p.annotation is OAOSContext:
                # inject via kw
                if p.name not in args:
                    args = dict(args)
                    args[p.name] = ctx
                return (), args
        return (), args
    except Exception:
        return (), args


# ---------------------------------------------------------------------------
# 3) ToolOutputLimits — truncate 4000, JSON schema check, auto retry
# ---------------------------------------------------------------------------

@dataclass
class ToolOutputLimits:
    """Limits applied to every tool output before feeding back to LLM.

    - truncate_at: max chars of tool content (default 4000, pydantic-ai style)
    - json_schema_check: if True and tool declares json_schema, validate output
    - max_retries: auto retry count when output violates schema or is truncated-ambiguous
    - suffix_on_truncate: marker appended when truncated
    """

    truncate_at: int = 4000
    json_schema_check: bool = True
    max_retries: int = 1
    suffix_on_truncate: str = "\n...[truncated]"

    def apply(self, content: str | Any, json_schema: dict[str, Any] | None = None) -> tuple[str, bool, str | None]:
        """Apply limits to tool output.

        Returns (content_str, should_retry, error_message)
        """
        # Normalize to string
        if not isinstance(content, str):
            try:
                content_str = json.dumps(content, ensure_ascii=False, default=str)
            except Exception:
                content_str = str(content)
        else:
            content_str = content

        truncated = False
        if len(content_str) > self.truncate_at:
            content_str = content_str[: self.truncate_at] + self.suffix_on_truncate
            truncated = True

        # JSON schema check — structural validation
        if self.json_schema_check and json_schema:
            # Only validate if content looks like JSON
            stripped = content_str.strip()
            # Remove truncation suffix for validation
            if truncated and stripped.endswith(self.suffix_on_truncate.strip()):
                stripped = stripped[: -len(self.suffix_on_truncate.strip())].strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    # Minimal schema check: required fields present, type checks
                    required = json_schema.get("required", [])
                    properties = json_schema.get("properties", {})
                    if isinstance(required, list) and isinstance(parsed, dict):
                        for req_key in required:
                            if req_key not in parsed:
                                return content_str, True, f"missing required field: {req_key}"
                    # type checks for properties if dict
                    if isinstance(properties, dict) and isinstance(parsed, dict):
                        for k, sch in properties.items():
                            if k in parsed and isinstance(sch, dict):
                                expected = sch.get("type")
                                if expected == "string" and not isinstance(parsed[k], str):
                                    return content_str, True, f"field {k} expected string"
                                if expected == "integer" and not isinstance(parsed[k], int):
                                    return content_str, True, f"field {k} expected integer"
                                if expected == "number" and not isinstance(parsed[k], (int, float)):
                                    return content_str, True, f"field {k} expected number"
                                if expected == "array" and not isinstance(parsed[k], list):
                                    return content_str, True, f"field {k} expected array"
                    # valid JSON and schema passed
                except json.JSONDecodeError as e:
                    return content_str, True, f"invalid JSON: {e}"
                except Exception as e:
                    return content_str, True, f"schema check error: {e}"

        # No hard retry for pure truncation — LLM can handle marker
        # Retry is driven by schema violation flag above
        return content_str, False, None


default_tool_limits = ToolOutputLimits()

# ---------------------------------------------------------------------------
# Tenant LLM quota (010) — Redis Lua atomic + DB + in-memory, production fail-closed
# Distributed: Redis Lua INCR+EXPIRE atomic is primary when REDIS_URL set (H5).
# Production (OAOS_ENV=production) fail-closed: Redis required, no in-memory fallback.
# Non-prod fallback preserved for tests (OAOS_ALLOW_TEST_FALLBACK or non-prod).
# External side effects NOT exactly-once; quota increment is atomic, external LLM
# calls are at-most-once per increment only (no rollback on downstream failure).
# ---------------------------------------------------------------------------
_llm_quota_store = {}
_llm_quota_window_counts = {}

# Redis Lua quota script — atomic daily+per-minute counter
_QUOTA_LUA_SCRIPT = """
local daily = KEYS[1]; local minute = KEYS[2]
local dlim = tonumber(ARGV[1]); local mlim = tonumber(ARGV[2])
local dc = redis.call('INCR', daily); if dc==1 then redis.call('EXPIRE', daily, 86400) end
local mc = redis.call('INCR', minute); if mc==1 then redis.call('EXPIRE', minute, 120) end
if dc > dlim then return {-1, dc, mc} end
if mc > mlim then return {-2, dc, mc} end
return {0, dc, mc}
"""

# For tests — allow injection of fakeredis client (overrides env URL)
_quota_redis_override = None
_quota_redis_override_url = None  # if set, use this url string for production checks

def set_quota_redis_client(client):  # test helper
    global _quota_redis_override
    _quota_redis_override = client

def clear_quota_redis_client():  # test helper
    global _quota_redis_override, _quota_redis_override_url
    _quota_redis_override = None
    _quota_redis_override_url = None

def _quota_redis_url() -> str | None:
    if _quota_redis_override_url is not None:
        return _quota_redis_override_url
    for k in ("OAOS_QUOTA_REDIS_URL", "OAOS_REDIS_URL", "REDIS_URL", "OAOS_CP_REDIS_URL", "OAOS_SESSION_REDIS_URL"):
        v = os.getenv(k, "").strip()
        if v:
            return v
    return None

def _allow_quota_fallback() -> bool:
    if _is_quota_production():
        return os.getenv("OAOS_ALLOW_TEST_FALLBACK", "").lower() in ("1", "true", "yes")
    return True

def _get_quota_redis_client():
    if _quota_redis_override is not None:
        return _quota_redis_override
    url = _quota_redis_url()
    if not url:
        return None
    try:
        import redis as _r  # type: ignore
        c = _r.Redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
        c.ping()
        return c
    except Exception as e:
        if _is_quota_production() and not _allow_quota_fallback():
            raise _quota_db_failure_exc(f"quota redis unavailable in production: {e}")
        # non-prod: let caller fall through to DB/memory with telemetry
        return None

def _quota_redis_eval(client, daily_key: str, minute_key: str, dlim: int, mlim: int):
    try:
        return client.eval(_QUOTA_LUA_SCRIPT, 2, daily_key, minute_key, dlim, mlim)
    except Exception as e:
        msg = str(e).lower()
        if "unknown command" in msg and "eval" in msg:
            # fakeredis without lupa fallback — emulate atomically
            dc = client.incr(daily_key)
            if dc == 1:
                try: client.expire(daily_key, 86400)
                except: pass
            mc = client.incr(minute_key)
            if mc == 1:
                try: client.expire(minute_key, 120)
                except: pass
            if dc > dlim:
                return [-1, dc, mc]
            if mc > mlim:
                return [-2, dc, mc]
            return [0, dc, mc]
        raise

def _get_quota_limits(tid: str) -> tuple[int, int]:
    rec = _llm_quota_store.get(tid)
    if rec is not None:
        return int(rec.get("daily_limit", 100)), int(rec.get("per_minute_limit", 10))
    return 100, 10

def _quota_http_exc(msg):
    try:
        from fastapi import HTTPException
        return HTTPException(status_code=429, detail={"code":"QUOTA_EXCEEDED","message":msg})
    except Exception:
        e=Exception(f"QUOTA_EXCEEDED: {msg}"); e.status_code=429; return e

def _quota_db_failure_exc(msg: str):
    try:
        from fastapi import HTTPException
        return HTTPException(status_code=503, detail={"code":"QUOTA_BACKEND_UNAVAILABLE","message":msg})
    except Exception:
        e=Exception(f"QUOTA_BACKEND_UNAVAILABLE: {msg}"); e.status_code=503; return e

def _is_quota_production() -> bool:
    try:
        from .env_gate import is_production as _is_prod
        return _is_prod()
    except Exception:
        return (os.getenv("OAOS_ENV","").lower() in ("production","prod"))

def _llm_quota_check(tenant_id):
    tid = (tenant_id or "default").strip() or "default"
    from datetime import datetime, timezone
    import os
    now = datetime.now(timezone.utc)
    # ── 1) Redis Lua primary (H5) — atomic daily+per-minute ──────────────
    rc = None
    try:
        rc = _get_quota_redis_client()
    except Exception as e:
        # production redis unavailable already raised as 503 inside helper
        raise
    if rc is not None:
        dlim, mlim = _get_quota_limits(tid)
        # If DB is configured and tenant has custom limits stored in DB, prefer those
        # Non-blocking best-effort: peek DB for limits without increment (fallback to mem defaults on error)
        db_url_for_limits = (os.getenv("OAOS_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
        if db_url_for_limits:
            try:
                from sqlalchemy import create_engine as _ce2  # type: ignore
                su = db_url_for_limits
                if su.startswith("postgresql+asyncpg://"): su = su.replace("postgresql+asyncpg://","postgresql+psycopg://",1)
                elif su.startswith("postgresql://"): su = su.replace("postgresql://","postgresql+psycopg://",1)
                if "+aiosqlite" in su: su = su.replace("+aiosqlite","")
                kwargs2: dict = {}
                if su.startswith("postgresql"): kwargs2 = {"pool_pre_ping": True, "connect_args": {"connect_timeout": 1}}
                eng2 = _ce2(su, **kwargs2)
                try:
                    from security.models.orm import AdminLLMQuotaORM as _Q  # type: ignore
                    from sqlalchemy.orm import sessionmaker as _sm2
                    fac2 = _sm2(bind=eng2, autoflush=False, autocommit=False)
                    with fac2() as s2:
                        row2 = s2.query(_Q).filter(_Q.tenant_id == tid).first()
                        if row2 is not None:
                            dlim = int(row2.daily_limit); mlim = int(row2.per_minute_limit)
                finally:
                    try: eng2.dispose()
                    except: pass
            except Exception:
                pass
        daily_key = f"oaos:quota:{tid}:daily:{now.strftime('%Y-%m-%d')}"
        minute_key = f"oaos:quota:{tid}:minute:{now.strftime('%Y-%m-%dT%H:%M')}"
        try:
            res = _quota_redis_eval(rc, daily_key, minute_key, dlim, mlim)
            code = int(res[0]) if isinstance(res, (list,tuple)) else int(res)
            if code == -1:
                raise _quota_http_exc("daily quota exceeded")
            if code == -2:
                raise _quota_http_exc("per-minute quota exceeded")
            return
        except Exception as e:
            if getattr(e, "status_code", None) == 429 or getattr(e, "status_code", None) == 503:
                raise
            try:
                detail = getattr(e, "detail", None)
                if isinstance(detail, dict) and detail.get("code") in ("QUOTA_EXCEEDED","QUOTA_BACKEND_UNAVAILABLE"):
                    raise
            except: pass
            if "QUOTA_EXCEEDED" in str(e) or "quota exceeded" in str(e).lower():
                raise
            if _is_quota_production() and not _allow_quota_fallback():
                raise _quota_db_failure_exc(f"quota redis backend unavailable: {e}")
            # non-prod fail-open -> fall through to DB/memory with telemetry
            try:
                from .env_gate import fail_open_telemetry
                fail_open_telemetry("quota","redis_failure_fail_open_nonprod", tenant_id=tid, error=str(e)[:200])
            except Exception:
                logger.warning("[fail-open] quota redis failure non-prod tenant=%s err=%s", tid, str(e)[:200])
            # fall through
    else:
        # No redis client available
        if _is_quota_production() and not _allow_quota_fallback():
            # In strict prod, redis is mandatory (H5); check if env expected redis
            url = _quota_redis_url()
            if url is None:
                # Explicitly require redis in prod — no in-memory bypass
                raise _quota_db_failure_exc("quota redis required in production but not configured (fail-closed)")
            # url was set but client failed (handled above as None only in non-prod); in prod we would have raised already
            raise _quota_db_failure_exc("quota redis unavailable in production (fail-closed)")
    db_url = (os.getenv("OAOS_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    # If DB is configured, try DB-backed quota first; production fail-closed on DB error
    if db_url:
        try:
            from sqlalchemy import create_engine as _ce, text as _t  # type: ignore
            sync_url = db_url
            if sync_url.startswith("postgresql+asyncpg://"):
                sync_url = sync_url.replace("postgresql+asyncpg://","postgresql+psycopg://",1)
            elif sync_url.startswith("postgresql://"):
                sync_url = sync_url.replace("postgresql://","postgresql+psycopg://",1)
            if "+aiosqlite" in sync_url:
                sync_url = sync_url.replace("+aiosqlite","")
            kwargs: dict = {"pool_pre_ping": True, "connect_args": {"connect_timeout": 2}} if sync_url.startswith("postgresql") else {}
            if sync_url.startswith("sqlite"):
                kwargs = {}
                if ":memory:" in sync_url:
                    kwargs["connect_args"] = {"check_same_thread": False}
            eng = _ce(sync_url, **kwargs)
            # ensure table
            try:
                from sqlalchemy import text as _tt
                ddl = "CREATE TABLE IF NOT EXISTS admin_llm_quotas (tenant_id TEXT PRIMARY KEY, daily_limit INTEGER NOT NULL DEFAULT 100, per_minute_limit INTEGER NOT NULL DEFAULT 10, used_today INTEGER NOT NULL DEFAULT 0, window_start TEXT, updated_at TEXT NOT NULL)"
                with eng.begin() as conn:
                    conn.execute(_tt(ddl))
            except Exception:
                pass
            from sqlalchemy.orm import sessionmaker as _sm  # type: ignore
            # Use ORM if available else raw
            try:
                from security.models.orm import AdminLLMQuotaORM  # type: ignore
                factory = _sm(bind=eng, autoflush=False, autocommit=False)
                with factory() as s:
                    row = s.query(AdminLLMQuotaORM).filter(AdminLLMQuotaORM.tenant_id == tid).first()
                    if row is None:
                        row = AdminLLMQuotaORM(tenant_id=tid, daily_limit=100, per_minute_limit=10, used_today=0, window_start=now, updated_at=now)
                        s.add(row); s.commit(); s.refresh(row)
                    if row.updated_at and row.updated_at.date() != now.date():
                        row.used_today = 0; row.window_start = now
                    wc = _llm_quota_window_counts.get(tid,0)
                    ws = row.window_start
                    if ws is None or (now - (ws if ws.tzinfo else ws.replace(tzinfo=timezone.utc))).total_seconds() >= 60:
                        wc = 0; row.window_start = now
                    if row.used_today >= row.daily_limit:
                        raise _quota_http_exc("daily quota exceeded")
                    if wc >= row.per_minute_limit:
                        raise _quota_http_exc("per-minute quota exceeded")
                    row.used_today += 1; wc += 1; _llm_quota_window_counts[tid]=wc; row.updated_at = now
                    s.commit()
                try: eng.dispose()
                except Exception: pass
                return
            except ImportError:
                # raw SQL fallback
                with eng.begin() as conn:
                    try:
                        conn.execute(_t("SELECT tenant_id FROM admin_llm_quotas LIMIT 1"))
                    except Exception:
                        pass
                try: eng.dispose()
                except Exception: pass
                # fall through to in-memory with telemetry
                if _is_quota_production():
                    raise _quota_db_failure_exc("quota DB unreachable — fail-closed in production")
                try:
                    from .env_gate import fail_open_telemetry
                    fail_open_telemetry("quota","db_orm_missing_fallback_to_memory", tenant_id=tid)
                except Exception:
                    logger.warning("[fail-open] quota db_orm_missing tenant=%s", tid)
        except Exception as e:
            # Preserve 429
            if getattr(e, "status_code", None) == 429 or "QUOTA_EXCEEDED" in str(e):
                raise
            if getattr(e, "status_code", None) == 503:
                raise
            try:
                detail = getattr(e, "detail", None)
                if isinstance(detail, dict) and detail.get("code") == "QUOTA_EXCEEDED":
                    raise
            except Exception:
                pass
            if _is_quota_production():
                logger.error("quota DB failure fail-closed tenant=%s err=%s", tid, str(e)[:300])
                raise _quota_db_failure_exc(f"quota backend unavailable: {e}")
            # non-prod fail-open with telemetry
            try:
                from .env_gate import fail_open_telemetry
                fail_open_telemetry("quota","db_failure_fail_open_nonprod", tenant_id=tid, error=str(e)[:200])
            except Exception:
                logger.warning("[fail-open] quota DB failure non-prod tenant=%s err=%s", tid, str(e)[:200])
            # fall through to in-memory
    # in-memory (per-replica) — non-prod fallback only; prod uses Redis Lua above (no fallback)
    rec=_llm_quota_store.get(tid)
    if rec is None:
        rec={"daily_limit":100,"per_minute_limit":10,"used_today":0,"window_start":now,"updated_at":now}
        _llm_quota_store[tid]=rec; _llm_quota_window_counts[tid]=0
    if rec["updated_at"].date()!=now.date():
        rec["used_today"]=0; rec["window_start"]=now; _llm_quota_window_counts[tid]=0
    wc=_llm_quota_window_counts.get(tid,0)
    if (now - rec["window_start"]).total_seconds()>=60:
        wc=0; rec["window_start"]=now
    if rec["used_today"] >= rec["daily_limit"]:
        raise _quota_http_exc("daily quota exceeded")
    if wc >= rec["per_minute_limit"]:
        raise _quota_http_exc("per-minute quota exceeded")
    rec["used_today"]+=1; _llm_quota_window_counts[tid]=wc+1; rec["updated_at"]=now

def _llm_quota_clear():
    _llm_quota_store.clear(); _llm_quota_window_counts.clear()
    # also flush redis quota keys for test isolation when using fakeredis override
    try:
        if _quota_redis_override is not None:
            _quota_redis_override.flushdb()
    except: pass

# ---------------------------------------------------------------------------
# LLM usage tracking (011) — latency/token/cost, tenant_id aggregated
# in-memory + DB persist (AdminLlmUsageORM), fail-open, 429 연동
# ---------------------------------------------------------------------------
import collections
from datetime import datetime, timezone as _tz

# pricing per 1k tokens (USD): prompt vs completion
_MODEL_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
    "gpt-4o": {"prompt": 0.0025, "completion": 0.01},
    "claude-3-5-sonnet": {"prompt": 0.003, "completion": 0.015},
    "claude": {"prompt": 0.003, "completion": 0.015},
    "gemini": {"prompt": 0.0005, "completion": 0.0015},
    "default": {"prompt": 0.001, "completion": 0.002},
}

def _estimate_cost_usd(prompt_tokens: int, completion_tokens: int, model: str | None = None) -> float:
    key = (model or "default").lower()
    # try exact then prefix match
    pricing = _MODEL_PRICING.get(key)
    if pricing is None:
        # prefix search
        for k, v in _MODEL_PRICING.items():
            if k in key or key in k:
                pricing = v
                break
        if pricing is None:
            pricing = _MODEL_PRICING["default"]
    return round((prompt_tokens / 1000.0) * pricing["prompt"] + (completion_tokens / 1000.0) * pricing["completion"], 6)

_llm_usage_records: collections.deque = collections.deque(maxlen=10000)

def _usage_db_url() -> str | None:
    url = os.getenv("OAOS_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
    return url.strip() or None

def _usage_ensure_table(engine) -> None:
    try:
        from security.models.orm import AdminLlmUsageORM  # type: ignore
        from security.models.db import Base  # type: ignore
        Base.metadata.create_all(bind=engine)
    except Exception:
        pass
    try:
        from sqlalchemy import text as _t
        ddl = """CREATE TABLE IF NOT EXISTS admin_llm_usage (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, provider TEXT, model TEXT,
            prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0, cost_usd FLOAT NOT NULL DEFAULT 0,
            latency_ms FLOAT NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'success',
            error TEXT, created_at TEXT NOT NULL)"""
        with engine.begin() as conn:
            conn.execute(_t(ddl))
    except Exception:
        pass

def _usage_normalize_sync_url(url: str) -> str:
    u = url.strip()
    if u.startswith("postgresql+asyncpg://"):
        u = u.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    elif u.startswith("postgresql://"):
        u = u.replace("postgresql://", "postgresql+psycopg://", 1)
    if "+aiosqlite" in u:
        u = u.replace("+aiosqlite", "")
        u = u.replace("sqlite+://", "sqlite://")
    if u.startswith("sqlite+"):
        u = u.replace("sqlite+", "sqlite", 1)
    return u

def _usage_db_insert(rec: dict) -> None:
    url = _usage_db_url()
    if not url:
        return
    try:
        from sqlalchemy import create_engine as _ce
        sync_url = _usage_normalize_sync_url(url)
        kwargs: dict = {"pool_pre_ping": True}
        if sync_url.startswith("sqlite"):
            kwargs = {}
            if ":memory:" in sync_url:
                kwargs["connect_args"] = {"check_same_thread": False}
        eng = _ce(sync_url, **kwargs)
        _usage_ensure_table(eng)
        from security.models.orm import AdminLlmUsageORM  # type: ignore
        from sqlalchemy.orm import sessionmaker as _sm
        factory = _sm(bind=eng, autoflush=False, autocommit=False)
        with factory() as s:
            orm = AdminLlmUsageORM(
                id=rec["id"], tenant_id=rec["tenant_id"], provider=rec.get("provider"),
                model=rec.get("model"), prompt_tokens=int(rec.get("prompt_tokens", 0)),
                completion_tokens=int(rec.get("completion_tokens", 0)), total_tokens=int(rec.get("total_tokens", 0)),
                cost_usd=float(rec.get("cost_usd", 0)), latency_ms=float(rec.get("latency_ms", 0)),
                status=str(rec.get("status", "success")), error=rec.get("error"), created_at=rec["created_at"],
            )
            s.add(orm)
            s.commit()
        try:
            eng.dispose()
        except Exception:
            pass
    except Exception:
        pass  # fail-open

def record_llm_usage(
    tenant_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: float = 0.0,
    status: str = "success",
    error: str | None = None,
    created_at: datetime | None = None,
) -> dict:
    tid = (tenant_id or "default").strip() or "default"
    pt = int(prompt_tokens or 0)
    ct = int(completion_tokens or 0)
    tt = pt + ct
    cost = _estimate_cost_usd(pt, ct, model)
    now = created_at or datetime.now(_tz.utc)
    rec = {
        "id": f"usage_{uuid.uuid4().hex[:12]}",
        "tenant_id": tid,
        "provider": provider,
        "model": model,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": tt,
        "cost_usd": cost,
        "latency_ms": float(latency_ms or 0),
        "status": status,
        "error": error,
        "created_at": now,
    }
    _llm_usage_records.append(rec)
    try:
        _usage_db_insert(rec)
    except Exception:
        pass
    return rec

def clear_llm_usage() -> None:
    _llm_usage_records.clear()
    url = _usage_db_url()
    if not url:
        return
    try:
        from sqlalchemy import create_engine as _ce, text as _t
        sync_url = _usage_normalize_sync_url(url)
        kwargs2: dict = {"pool_pre_ping": True}
        if sync_url.startswith("sqlite"):
            kwargs2 = {}
            if ":memory:" in sync_url:
                kwargs2["connect_args"] = {"check_same_thread": False}
        eng = _ce(sync_url, **kwargs2)
        with eng.begin() as conn:
            try:
                conn.execute(_t("DELETE FROM admin_llm_usage"))
            except Exception:
                pass
        try:
            eng.dispose()
        except Exception:
            pass
    except Exception:
        pass

def _usage_to_public(r: dict) -> dict:
    ca = r.get("created_at")
    if isinstance(ca, datetime):
        iso = ca.isoformat()
    else:
        iso = str(ca) if ca else ""
    return {
        "id": r.get("id"), "tenant_id": r.get("tenant_id"), "provider": r.get("provider"),
        "model": r.get("model"), "prompt_tokens": r.get("prompt_tokens", 0),
        "completion_tokens": r.get("completion_tokens", 0), "total_tokens": r.get("total_tokens", 0),
        "cost_usd": r.get("cost_usd", 0), "latency_ms": r.get("latency_ms", 0),
        "status": r.get("status", "success"), "error": r.get("error"), "created_at": iso,
    }

def get_llm_usage_history(limit: int = 20, tenant_id: str | None = None) -> list[dict]:
    lim = max(1, min(1000, int(limit or 20)))
    tid = (tenant_id or "").strip() if tenant_id is not None else None
    # try in-memory first
    recs = list(_llm_usage_records)
    if tid:
        recs = [r for r in recs if r.get("tenant_id") == tid]
    # if empty and DB configured, try DB fallback
    if not recs:
        url = _usage_db_url()
        if url:
            try:
                from sqlalchemy import create_engine as _ce
                sync_url = _usage_normalize_sync_url(url)
                kwargs3: dict = {"pool_pre_ping": True}
                if sync_url.startswith("sqlite"):
                    kwargs3 = {}
                    if ":memory:" in sync_url:
                        kwargs3["connect_args"] = {"check_same_thread": False}
                eng = _ce(sync_url, **kwargs3)
                _usage_ensure_table(eng)
                from security.models.orm import AdminLlmUsageORM  # type: ignore
                from sqlalchemy.orm import sessionmaker as _sm2
                factory = _sm2(bind=eng, autoflush=False, autocommit=False)
                with factory() as s:
                    q = s.query(AdminLlmUsageORM).order_by(AdminLlmUsageORM.created_at.desc())
                    if tid:
                        q = q.filter(AdminLlmUsageORM.tenant_id == tid)
                    rows = q.limit(lim).all()
                    recs = [
                        {"id": r.id, "tenant_id": r.tenant_id, "provider": r.provider, "model": r.model,
                         "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
                         "total_tokens": r.total_tokens, "cost_usd": r.cost_usd, "latency_ms": r.latency_ms,
                         "status": r.status, "error": r.error, "created_at": r.created_at}
                        for r in rows
                    ]
                try:
                    eng.dispose()
                except Exception:
                    pass
            except Exception:
                pass
    # sort newest first
    recs_sorted = sorted(recs, key=lambda x: x.get("created_at") or datetime.min.replace(tzinfo=_tz.utc), reverse=True)
    return [_usage_to_public(r) for r in recs_sorted[:lim]]

def get_llm_usage_summary(tenant_id: str | None = None) -> dict:
    tid = (tenant_id or "").strip() if tenant_id is not None else None
    # collect records: in-memory + maybe DB if empty
    recs = list(_llm_usage_records)
    if tid:
        recs = [r for r in recs if r.get("tenant_id") == tid]
    if not recs:
        url = _usage_db_url()
        if url:
            try:
                from sqlalchemy import create_engine as _ce
                sync_url = _usage_normalize_sync_url(url)
                kw: dict = {"pool_pre_ping": True}
                if sync_url.startswith("sqlite"):
                    kw = {}
                    if ":memory:" in sync_url:
                        kw["connect_args"] = {"check_same_thread": False}
                eng = _ce(sync_url, **kw)
                _usage_ensure_table(eng)
                from security.models.orm import AdminLlmUsageORM  # type: ignore
                from sqlalchemy.orm import sessionmaker as _sm3
                factory = _sm3(bind=eng, autoflush=False, autocommit=False)
                with factory() as s:
                    q = s.query(AdminLlmUsageORM)
                    if tid:
                        q = q.filter(AdminLlmUsageORM.tenant_id == tid)
                    rows = q.all()
                    recs = [
                        {"id": r.id, "tenant_id": r.tenant_id, "provider": r.provider, "model": r.model,
                         "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
                         "total_tokens": r.total_tokens, "cost_usd": r.cost_usd, "latency_ms": r.latency_ms,
                         "status": r.status, "error": r.error, "created_at": r.created_at}
                        for r in rows
                    ]
                try:
                    eng.dispose()
                except Exception:
                    pass
            except Exception:
                pass
    total = len(recs)
    if total == 0:
        return {"tenant_id": tid or "all", "total_requests": 0, "success_count": 0, "fail_count": 0,
                "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "total_cost_usd": 0.0, "avg_latency_ms": 0.0, "p95_latency_ms": 0.0,
                "daily_count": 0, "per_minute_count": 0, "daily_cost_usd": 0.0}
    success = sum(1 for r in recs if r.get("status") == "success")
    fail = total - success
    total_tokens = sum(int(r.get("total_tokens", 0)) for r in recs)
    prompt_tokens = sum(int(r.get("prompt_tokens", 0)) for r in recs)
    completion_tokens = sum(int(r.get("completion_tokens", 0)) for r in recs)
    total_cost = round(sum(float(r.get("cost_usd", 0)) for r in recs), 6)
    latencies = sorted(float(r.get("latency_ms", 0)) for r in recs)
    avg_lat = round(sum(latencies) / len(latencies), 2) if latencies else 0.0
    # p95
    import math
    if latencies:
        idx = math.ceil(0.95 * len(latencies)) - 1
        idx = max(0, min(idx, len(latencies)-1))
        p95 = round(latencies[idx], 2)
    else:
        p95 = 0.0
    # daily/per-minute windows (UTC)
    now = datetime.now(_tz.utc)
    daily = [r for r in recs if r.get("created_at") and r["created_at"].date() == now.date()]
    per_min = [r for r in recs if r.get("created_at") and (now - r["created_at"]).total_seconds() < 60]
    daily_cost = round(sum(float(r.get("cost_usd", 0)) for r in daily), 6)
    return {
        "tenant_id": tid or "all", "total_requests": total, "success_count": success, "fail_count": fail,
        "total_tokens": total_tokens, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
        "total_cost_usd": total_cost, "avg_latency_ms": avg_lat, "p95_latency_ms": p95,
        "daily_count": len(daily), "per_minute_count": len(per_min), "daily_cost_usd": daily_cost,
        "daily_tokens": sum(int(r.get("total_tokens", 0)) for r in daily),
        "per_minute_tokens": sum(int(r.get("total_tokens", 0)) for r in per_min),
    }

# ---------------------------------------------------------------------------
# Shared helpers for provider layer (mock, litellm lazy, audit, retry)
# ---------------------------------------------------------------------------

_LITELLM_AVAILABLE: bool | None = None


def _load_litellm() -> Any | None:
    global _LITELLM_AVAILABLE
    if _LITELLM_AVAILABLE is not None:
        try:
            import litellm as _lm  # type: ignore

            return _lm
        except ImportError:
            return None
    try:
        import litellm as _lm  # type: ignore

        _LITELLM_AVAILABLE = True
        return _lm
    except ImportError:
        _LITELLM_AVAILABLE = False
        return None


def _is_mock_allowed() -> bool:
    try:
        from .env_gate import is_mock_allowed as _gate_mock
        return _gate_mock()
    except Exception:
        mf = os.getenv("OAOS_MOCK_FALLBACK", "").lower()
        if mf in ("1", "true", "yes", "on"):
            return True
        if mf in ("0", "false", "no", "off"):
            return False
        if os.getenv("OAOS_ENV", "").lower() in ("production", "prod"):
            return False
        return True


def _mock_completion_response(model: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = str(m.get("content", ""))[:200]
            break
    return {
        "id": f"mock-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"[mock:{model}] echo: {last_user}" if last_user else f"[mock:{model}] hello",
                    "tool_calls": [],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _mock_stream_chunks(model: str, content: str = "mock stream") -> list[dict[str, Any]]:
    words = content.split()
    chunks: list[dict[str, Any]] = []
    for i, w in enumerate(words):
        chunks.append(
            {
                "id": f"mock-stream-{uuid.uuid4().hex[:6]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": w + " "}, "finish_reason": None}],
            }
        )
    chunks.append(
        {
            "id": f"mock-stream-{uuid.uuid4().hex[:6]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    return chunks


AuditHook = Callable[[dict[str, Any]], None]


@dataclass
class AuditEvent:
    event_type: str
    trace_id: str
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    session_id: str = ""
    request_id: str = ""
    model: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditLogStub:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self._hooks: list[AuditHook] = []

    def add_hook(self, hook: AuditHook) -> None:
        self._hooks.append(hook)

    def emit(self, event: AuditEvent | dict[str, Any]) -> AuditEvent:
        if isinstance(event, dict):
            ev = AuditEvent(
                event_type=str(event.get("event_type", "unknown")),
                trace_id=str(event.get("trace_id", "")),
                session_id=str(event.get("session_id", "")),
                request_id=str(event.get("request_id", "")),
                model=str(event.get("model", "")),
                data=dict(event.get("data") or {}),
            )
        else:
            ev = event
        self.events.append(ev)
        for h in self._hooks:
            try:
                h(ev.to_dict())
            except Exception:
                pass
        return ev

    def query(self, trace_id: str | None = None, event_type: str | None = None) -> list[AuditEvent]:
        out = self.events
        if trace_id:
            out = [e for e in out if e.trace_id == trace_id]
        if event_type:
            out = [e for e in out if e.event_type == event_type]
        return out

    def clear(self) -> None:
        self.events.clear()


default_audit_log = AuditLogStub()


async def _with_timeout(coro: Awaitable[Any], timeout_s: float | None) -> Any:
    if timeout_s is None or timeout_s <= 0:
        return await coro
    return await asyncio.wait_for(coro, timeout=timeout_s)


# -- HA: retry eligibility (only 500/429/timeout) + circuit breaker + audit --
def _is_retryable_exception(exc: BaseException) -> bool:
    # timeout
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    # httpx timeout
    try:
        import httpx as _hx  # type: ignore
        if isinstance(exc, _hx.TimeoutException):  # type: ignore
            return True
    except Exception:
        pass
    # status code based
    for attr in ("status_code", "status", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and v in (429, 500, 502, 503, 504):
            return True
        if isinstance(v, str) and v in ("429", "500"):
            return True
    # httpx HTTPStatusError
    resp = getattr(exc, "response", None)
    if resp is not None:
        sc = getattr(resp, "status_code", None)
        if sc in (429, 500, 502, 503, 504):
            return True
    msg = str(exc).lower()
    if "429" in msg or "too many requests" in msg:
        return True
    if "timeout" in msg or "timed out" in msg:
        return True
    if any(x in msg for x in ("500", "502", "503", "504", "internal server error", "service unavailable")):
        # only if explicitly 5xx-like
        return True
    return False


class CircuitBreaker:
    """Simple circuit breaker: CLOSED -> OPEN after threshold failures, half-open after reset_timeout."""

    def __init__(self, failure_threshold: int = 3, reset_timeout_s: float = 30.0, name: str = "default"):
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self.name = name
        self._failures: int = 0
        self._state: str = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self._opened_at: float | None = None
        self._lock = asyncio.Lock() if False else None  # avoid async lock in sync context; use simple

    def can_execute(self) -> bool:
        if self._state == "CLOSED":
            return True
        if self._state == "OPEN":
            if self._opened_at is not None and (time.monotonic() - self._opened_at) >= self.reset_timeout_s:
                self._state = "HALF_OPEN"
                return True
            return False
        # HALF_OPEN allows one probe
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._state = "CLOSED"
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = "OPEN"
            self._opened_at = time.monotonic()

    @property
    def state(self) -> str:
        # auto-transition check
        if self._state == "OPEN" and self._opened_at is not None and (time.monotonic() - self._opened_at) >= self.reset_timeout_s:
            self._state = "HALF_OPEN"
        return self._state


_default_circuit_breaker = CircuitBreaker(failure_threshold=3, reset_timeout_s=30.0, name="llm_runtime")
# also per-model breakers
_circuit_breakers: dict[str, CircuitBreaker] = {}


def _get_circuit_breaker(key: str = "default") -> CircuitBreaker:
    if key not in _circuit_breakers:
        _circuit_breakers[key] = CircuitBreaker(failure_threshold=3, reset_timeout_s=30.0, name=key)
    return _circuit_breakers[key]


async def _with_retry(
    fn: Callable[[], Awaitable[Any]],
    *,
    max_retries: int = 3,
    backoff_s: float = 0.5,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    observability_hook: AuditHook | None = None,
    trace_id: str = "",
    audit_log: Any | None = None,
    circuit_breaker: CircuitBreaker | None = None,
) -> Any:
    last_exc: BaseException | None = None
    # circuit check before first attempt
    cb = circuit_breaker or _default_circuit_breaker
    if not cb.can_execute():
        err = RuntimeError(f"circuit breaker OPEN for {cb.name}")
        if audit_log is not None:
            try:
                audit_log.emit({"event_type": "circuit_breaker_open", "trace_id": trace_id, "data": {"breaker": cb.name, "state": cb.state}})
            except Exception:
                pass
        raise err
    for attempt in range(max_retries + 1):
        try:
            result = await fn()
            cb.record_success()
            return result
        except retry_on as e:
            # only retry if eligible (500/429/timeout)
            if not _is_retryable_exception(e):
                cb.record_failure()
                if audit_log is not None:
                    try:
                        audit_log.emit({"event_type": "llm_failure", "trace_id": trace_id, "data": {"error": str(e)[:500], "retryable": False, "attempt": attempt + 1}})
                    except Exception:
                        pass
                raise
            last_exc = e
            if attempt >= max_retries:
                break
            delay = backoff_s * (2**attempt)
            if observability_hook:
                try:
                    observability_hook(
                        {
                            "event_type": "retry",
                            "trace_id": trace_id,
                            "data": {"attempt": attempt + 1, "max_retries": max_retries, "error": str(e), "backoff_s": delay},
                        }
                    )
                except Exception:
                    pass
            if audit_log is not None:
                try:
                    audit_log.emit({"event_type": "retry", "trace_id": trace_id, "data": {"attempt": attempt + 1, "max_retries": max_retries, "error": str(e)[:300], "backoff_s": delay}})
                except Exception:
                    pass
            await asyncio.sleep(delay)
    assert last_exc is not None
    # final failure audit
    cb.record_failure()
    if audit_log is not None:
        try:
            audit_log.emit({"event_type": "llm_failure", "trace_id": trace_id, "data": {"error": str(last_exc)[:500], "retryable": True, "attempts": max_retries + 1, "breaker_state": cb.state}})
        except Exception:
            pass
    raise last_exc


# ---------------------------------------------------------------------------
# 2) output_type handling — BaseModel validation with retry (max 2)
# ---------------------------------------------------------------------------

def _extract_content(response: dict[str, Any]) -> str:
    try:
        choices = response.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return str(msg.get("content") or "")
    except Exception:
        return ""


def _output_type_retry_prompt(original_content: str, error: str, model_name: str) -> dict[str, Any]:
    """Build correction message when output_type validation fails."""
    return {
        "role": "user",
        "content": f"Your previous response failed validation for {model_name}: {error}. The raw output was: {original_content[:2000]}. Please return ONLY valid JSON matching the expected schema, with no extra text.",
    }


async def _validate_and_retry_output(
    llm: "LLMProviderAdapter",
    messages: list[dict[str, Any]],
    response: dict[str, Any],
    output_type: type[BaseModel],
    trace_id: str,
    request_id: str,
    tools: list[dict[str, Any]] | None,
    llm_kwargs: dict[str, Any],
    max_retries: int = 2,
) -> tuple[dict[str, Any], BaseModel | None]:
    """Validate response content against output_type. Retry up to max_retries with correction prompt.

    Returns (final_response, parsed_model_or_None). On success, parsed model is validated.
    On final failure, raises ValidationError so caller can handle.
    """
    last_err: str | None = None
    current_resp = response
    history = list(messages)
    # Prepare schema for error messages
    try:
        schema_name = getattr(output_type, "__name__", str(output_type))
        json_schema = output_type.model_json_schema() if hasattr(output_type, "model_json_schema") else {}
    except Exception:
        schema_name = str(output_type)
        json_schema = {}

    for attempt in range(max_retries + 1):
        content = _extract_content(current_resp)
        # Try to parse — handle both pure JSON and wrapped content
        parsed_value: Any | None = None
        validation_error: str | None = None
        # Extract JSON substring if LLM wrapped with text
        candidate = content.strip()
        # If content contains JSON inside, extract first {..} or [..]
        if candidate and not (candidate.startswith("{") or candidate.startswith("[")):
            # Try to find JSON object
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidate = candidate[start : end + 1]
        try:
            if isinstance(candidate, str) and candidate.strip().startswith(("{", "[")):
                data = json.loads(candidate)
            else:
                # Fallback: treat content as raw string -> will fail validation to trigger retry
                data = json.loads(candidate) if candidate else {}
            # Validate via Pydantic
            if isinstance(data, dict):
                parsed = output_type.model_validate(data)
            elif isinstance(data, list):
                # For list outputs, validate via TypeAdapter if output_type is not list
                parsed = output_type.model_validate(data)  # type: ignore
            else:
                parsed = output_type.model_validate(data)  # type: ignore
            # Success — annotate response with parsed model
            current_resp["_output_type_validated"] = True
            current_resp["_parsed_output"] = parsed
            return current_resp, parsed
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            validation_error = str(e)
            # Include schema hint on last attempt
            last_err = validation_error
            if attempt >= max_retries:
                # Final failure: emit and raise
                llm._emit("output_type_validation_failed", trace_id=trace_id, model=llm.resolve_model(llm_kwargs.get("model")), data={"attempt": attempt + 1, "error": validation_error, "schema": schema_name})
                # Attach error to response for caller inspection
                current_resp["_output_type_error"] = validation_error
                current_resp["_output_type_schema"] = json_schema
                # Raise validation error so StructuredToolLoop can handle, but also return response
                # We don't raise here for adapter direct use — instead let caller decide
                # For strictness, we store error and return; caller can check _parsed_output
                # However to satisfy "retry with correction" we retry below if attempts left
                # If final, we keep response with error marker; optionally raise
                # We choose to keep response and NOT raise, so loop can handle gracefully
                # But for direct adapter test that expects validation, we surface via response
                return current_resp, None
            # Retry: append correction prompt and re-call LLM
            correction = _output_type_retry_prompt(content, validation_error, schema_name)
            retry_messages = history + [{"role": "assistant", "content": content}] + [correction]
            llm._emit("output_type_retry", trace_id=trace_id, model=llm.resolve_model(llm_kwargs.get("model")), data={"attempt": attempt + 1, "max_retries": max_retries, "error": validation_error})
            # Re-call LLM without output_type to avoid infinite recursion (we handle validation externally)
            try:
                # Call underlying completion without output_type to avoid recursion
                retry_kwargs = dict(llm_kwargs)
                retry_kwargs.pop("output_type", None)
                # Use internal method to avoid re-entering validation
                current_resp = await llm._raw_completion(retry_messages, tools=tools, trace_id=trace_id, request_id=request_id, **retry_kwargs)
                history = retry_messages
            except Exception as e2:
                current_resp["_output_type_error"] = f"retry failed: {e2}; original: {validation_error}"
                return current_resp, None
        except Exception as e:
            last_err = str(e)
            current_resp["_output_type_error"] = last_err
            return current_resp, None

    current_resp["_output_type_error"] = last_err or "unknown validation error"
    return current_resp, None


# ---------------------------------------------------------------------------
# 1) LLMProviderAdapter — litellm wrapper with OAOSContext, output_type, limits
# ---------------------------------------------------------------------------

@dataclass
class ModelRouting:
    default_model: str = "gpt-4o-mini"
    routes: dict[str, str] = field(default_factory=dict)

    def resolve(self, model: str | None) -> str:
        if not model:
            return self.default_model
        return self.routes.get(model, model)


class LLMProviderAdapter:
    """Litellm wrapper with model routing, streaming, retry/timeout, observability.

    Enhanced:
      - OAOSContext propagation via headers/trace
      - output_type: BaseModel validation with max 2 retries
      - ToolOutputLimits integration for downstream tool loop
    """

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        routing: dict[str, str] | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        retry_backoff_s: float = 0.5,
        api_key: str | None = None,
        observability_hook: AuditHook | None = None,
        audit_log: AuditLogStub | None = None,
        mock_responses: list[dict[str, Any]] | None = None,
        tool_output_limits: ToolOutputLimits | None = None,
        provider: str | ProviderType | None = None,
        provider_type: str | ProviderType | None = None,
        base_url: str | None = None,
        runtime_mode: str | RuntimeMode | None = None,
        hermes_api_url: str | None = None,
        provider_config: dict[str, Any] | None = None,
    ) -> None:
        # Resolve provider_type: explicit kw > env > None (keeps litellm path backwards compat)
        _pt = provider_type if provider_type is not None else provider
        if isinstance(_pt, str) and _pt:
            self.provider_type: ProviderType | None = ProviderType.from_str(_pt)
        elif isinstance(_pt, ProviderType):
            self.provider_type = _pt
        else:
            self.provider_type = _resolve_provider_from_env()
        # Allow env/model hint to infer provider when not explicitly set but env says so
        if self.provider_type is None and provider_config and provider_config.get("provider_type"):
            self.provider_type = ProviderType.from_str(str(provider_config.get("provider_type")))
        if self.provider_type is None and provider_config and provider_config.get("provider"):
            self.provider_type = ProviderType.from_str(str(provider_config.get("provider")))
        self.base_url = base_url
        # Runtime mode: hermes delegates to Hermes Agent, llm uses direct provider dispatch
        self.runtime_mode: RuntimeMode = _resolve_runtime_mode(runtime_mode)
        # If provider explicitly set, force llm mode unless hermes explicitly requested
        # But per spec: if mode == hermes, NO provider config should be used — bypass dispatch
        # So we respect hermes mode strictly
        self.hermes_api_url = hermes_api_url or os.getenv("HERMES_API_URL") or os.getenv("OAOS_HERMES_API_URL") or os.getenv("OAOS_HERMES_BASE_URL") or "http://localhost:8001"
        # Provider config resolution: admin-console API merged over env, unless hermes mode (skip)
        if self.runtime_mode == RuntimeMode.HERMES:
            self.provider_config: dict[str, Any] = {}
        else:
            if provider_config is not None:
                self.provider_config = dict(provider_config)
            else:
                # fetch merged config (admin API + env) only when provider_type known or need generic
                self.provider_config = _resolve_provider_config(self.provider_type, prefer_admin_api=True)
                # also store generic api_key/base_url resolution
                if api_key:
                    self.provider_config["api_key"] = api_key
                if base_url:
                    self.provider_config["base_url"] = base_url
        self.routing = ModelRouting(default_model=model, routes=routing or {})
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self.api_key = api_key or self.provider_config.get("api_key") or self.provider_config.get(f"{str(self.provider_type).lower()}_api_key") if self.provider_type else api_key
        self.observability_hook = observability_hook
        self.audit_log = audit_log or default_audit_log
        self._mock_responses: list[dict[str, Any]] = list(mock_responses or [])
        self._mock_index: int = 0
        self.tool_output_limits = tool_output_limits or default_tool_limits
        self._provider_instance: Any | None = None

    def resolve_model(self, model: str | None) -> str:
        return self.routing.resolve(model)

    def _emit(self, event_type: str, trace_id: str = "", data: dict[str, Any] | None = None, model: str = "") -> None:
        payload: dict[str, Any] = {"event_type": event_type, "trace_id": trace_id, "model": model, "data": data or {}}
        if self.audit_log is not None:
            try:
                self.audit_log.emit(payload)
            except Exception:
                pass
        if self.observability_hook is not None:
            try:
                self.observability_hook(payload)
            except Exception:
                pass

    def push_mock_response(self, response: dict[str, Any]) -> None:
        self._mock_responses.append(response)

    def _next_mock(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        if self._mock_index < len(self._mock_responses):
            resp = self._mock_responses[self._mock_index]
            self._mock_index += 1
            if "model" not in resp:
                resp = dict(resp)
                resp["model"] = model
            return resp
        if tools:
            pass
        return _mock_completion_response(model, messages, tools=tools)

    def _check_quota(self, tenant_id: str | None = None, oaos_context: Any | None = None) -> None:
        # quota hook before provider dispatch — fail-open on DB missing
        tid = ""
        if oaos_context is not None and hasattr(oaos_context, "tenant_id"):
            tid = str(getattr(oaos_context, "tenant_id") or "")
        if not tid:
            tid = str(tenant_id or "default")
        if not tid.strip():
            tid = "default"
        try:
            _llm_quota_check(tid)
        except Exception as e:
            # re-raise quota 429 (has code QUOTA_EXCEEDED), otherwise fail-open
            msg = str(e)
            if "QUOTA_EXCEEDED" in msg or getattr(e, "status_code", None) == 429:
                raise
            # also check HTTPException detail
            try:
                detail = getattr(e, "detail", None)
                if isinstance(detail, dict) and detail.get("code") == "QUOTA_EXCEEDED":
                    raise
            except Exception:
                pass
            # fail-open for DB errors
            return

    def _get_provider_instance(self) -> Any | None:
        """Lazy instantiate provider for current provider_type — returns None if no provider_type."""
        if self.provider_type is None:
            return None
        if self._provider_instance is not None:
            return self._provider_instance
        # Lazy import registry — avoid hard deps at import time
        try:
            from .providers import get_provider as _get_provider
            # Build provider-specific config
            pkey = str(self.provider_type.value)
            api_key = self.api_key or self.provider_config.get("api_key") or self.provider_config.get(f"{pkey}_api_key") or ""
            base_url = self.base_url or self.provider_config.get("base_url") or self.provider_config.get(f"{pkey}_base_url")
            model_cfg = self.provider_config.get(f"{pkey}_model") or self.provider_config.get("model") or None
            cfg: dict[str, Any] = {}
            if api_key:
                cfg["api_key"] = api_key
            if base_url:
                cfg["base_url"] = base_url
            if model_cfg:
                cfg["model"] = model_cfg
            self._provider_instance = _get_provider(pkey, cfg)
            return self._provider_instance
        except Exception:
            return None

    async def _hermes_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        trace_id: str = "",
        request_id: str = "",
        oaos_context: OAOSContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Delegate to Hermes Agent API when runtime_mode == hermes."""
        resolved = self.resolve_model(model)
        # Hermes endpoint: POST {hermes_api_url}/v1/chat/completions or /acp/sessions/.../prompt
        # We try OpenAI-compat first, then ACP-style
        try:
            import httpx  # type: ignore
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if oaos_context is not None:
                headers.update(oaos_context.to_headers())
            if trace_id:
                headers["X-Trace-Id"] = trace_id
            payload: dict[str, Any] = {"model": resolved, "messages": messages, "stream": False}
            if tools:
                payload["tools"] = tools
            # allow extra kwargs
            for k in ("temperature", "max_tokens", "top_p"):
                if k in kwargs:
                    payload[k] = kwargs[k]
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                # Try OpenAI-compat
                for path in ("/v1/chat/completions", "/v1/completions", "/acp/chat/completions"):
                    try:
                        url = self.hermes_api_url.rstrip("/") + path
                        r = await client.post(url, json=payload, headers=headers)
                        if r.status_code < 400:
                            data = r.json()
                            if "choices" in data:
                                data.setdefault("object", "chat.completion")
                                data.setdefault("model", resolved)
                                return data
                    except Exception:
                        continue
                if not _is_mock_allowed():
                    raise RuntimeError("LLM provider unavailable — mock fallback disabled in production (OAOS_ENV=production or OAOS_MOCK_FALLBACK=0)")
                return _mock_completion_response(resolved, messages, **kwargs)
        except Exception:
            if not _is_mock_allowed():
                raise
            return _mock_completion_response(resolved, messages, **kwargs)

    async def _raw_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        trace_id: str = "",
        request_id: str = "",
        oaos_context: OAOSContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Internal completion without output_type handling — single LLM call.
        Dispatch order: hermes mode -> provider dispatch -> litellm/mock.
        """
        resolved = self.resolve_model(model)
        # Propagate OAOSContext trace if provided
        if oaos_context is not None and not trace_id:
            trace_id = oaos_context.trace_id

        self._emit("model_request", trace_id=trace_id, model=resolved, data={"request_id": request_id, "messages_len": len(messages), "provider": str(self.provider_type.value) if self.provider_type else "litellm", "runtime_mode": str(self.runtime_mode.value)})
        # quota hook before provider dispatch (fail-open) — also record 429 as failed usage
        tenant_for_quota = getattr(oaos_context, "tenant_id", None) if oaos_context is not None else kwargs.get("tenant_id")
        _usage_tid = (tenant_for_quota or getattr(oaos_context, "tenant_id", None) or kwargs.get("tenant_id") or "default")
        _usage_start = time.perf_counter()
        _usage_provider = str(self.provider_type.value) if self.provider_type else "litellm"
        def _extract_usage(resp: dict | None):
            if not isinstance(resp, dict):
                return 0, 0
            u = resp.get("usage") or {}
            try:
                pt = int(u.get("prompt_tokens", 0) or 0)
                ct = int(u.get("completion_tokens", 0) or 0)
                if pt == 0 and ct == 0:
                    # fallback to total_tokens
                    tt = int(u.get("total_tokens", 0) or 0)
                    if tt:
                        pt = tt // 2
                        ct = tt - pt
                return pt, ct
            except Exception:
                return 0, 0
        def _record_success(resp: dict | None, latency: float):
            try:
                pt, ct = _extract_usage(resp)
                # try to get model from response
                m = (resp.get("model") if isinstance(resp, dict) else None) or resolved
                p = _usage_provider
                # if litellm path, try litellm model, else provider value
                record_llm_usage(tenant_id=_usage_tid, provider=p, model=m, prompt_tokens=pt, completion_tokens=ct, latency_ms=round(latency*1000,2), status="success")
            except Exception:
                pass
        def _record_fail(err: str, latency: float):
            try:
                record_llm_usage(tenant_id=_usage_tid, provider=_usage_provider, model=resolved, prompt_tokens=0, completion_tokens=0, latency_ms=round(latency*1000,2), status="failed", error=str(err)[:500])
            except Exception:
                pass
        try:
            _llm_quota_check(tenant_for_quota or "default")
        except Exception as e:
            if getattr(e, "status_code", None)==429 or "QUOTA_EXCEEDED" in str(e) or (isinstance(getattr(e, "detail", None), dict) and getattr(e, "detail", {}).get("code")=="QUOTA_EXCEEDED"):
                _record_fail(str(e) or "quota exceeded", time.perf_counter() - _usage_start)
                raise
        # — Hermes mode: bypass provider logic entirely —
        if self.runtime_mode == RuntimeMode.HERMES:
            async def _do_hermes() -> dict[str, Any]:
                # Mock queue takes priority if mock_responses set
                if self._mock_responses and self._mock_index < len(self._mock_responses):
                    return self._next_mock(resolved, messages, tools)
                return await self._hermes_completion(messages, model=model, tools=tools, trace_id=trace_id, request_id=request_id, oaos_context=oaos_context, **kwargs)
            try:
                result = await _with_timeout(
                    _with_retry(
                        _do_hermes,
                        max_retries=self.max_retries,
                        backoff_s=self.retry_backoff_s,
                        observability_hook=self.observability_hook,
                        trace_id=trace_id,
                        audit_log=self.audit_log,
                        circuit_breaker=_get_circuit_breaker(f"hermes:{resolved}"),
                    ),
                    timeout_s=self.timeout_s,
                )
                self._emit("model_response", trace_id=trace_id, model=resolved, data={"request_id": request_id, "runtime_mode": "hermes", "finish_reason": result.get("choices", [{}])[0].get("finish_reason", "") if isinstance(result, dict) else ""})
                _record_success(result, time.perf_counter() - _usage_start)
                return result  # type: ignore
            except asyncio.TimeoutError as e:
                self._emit("error", trace_id=trace_id, model=resolved, data={"error": "timeout", "timeout_s": self.timeout_s})
                _record_fail("timeout", time.perf_counter() - _usage_start)
                raise TimeoutError(f"LLM completion timeout after {self.timeout_s}s") from e
            except Exception as e:
                self._emit("error", trace_id=trace_id, model=resolved, data={"error": str(e)})
                _record_fail(str(e), time.perf_counter() - _usage_start)
                raise

        # — Provider dispatch (when provider_type set) —
        if self.provider_type is not None:
            prov_instance = self._get_provider_instance()
            if prov_instance is not None:
                async def _do_provider() -> dict[str, Any]:
                    if self._mock_responses and self._mock_index < len(self._mock_responses):
                        return self._next_mock(resolved, messages, tools)
                    # provider call() is async
                    try:
                        return await prov_instance.call(messages, model=resolved, tools=tools, trace_id=trace_id, request_id=request_id, **kwargs)
                    except TypeError:
                        # fallback without extra kwargs
                        return await prov_instance.call(messages, model=resolved, tools=tools, **kwargs)
                try:
                    result = await _with_timeout(
                        _with_retry(
                            _do_provider,
                            max_retries=self.max_retries,
                            backoff_s=self.retry_backoff_s,
                            observability_hook=self.observability_hook,
                            trace_id=trace_id,
                            audit_log=self.audit_log,
                            circuit_breaker=_get_circuit_breaker(f"provider:{resolved}"),
                        ),
                        timeout_s=self.timeout_s,
                    )
                    self._emit("model_response", trace_id=trace_id, model=resolved, data={"request_id": request_id, "provider": str(self.provider_type.value), "finish_reason": result.get("choices", [{}])[0].get("finish_reason", "") if isinstance(result, dict) else ""})
                    _record_success(result, time.perf_counter() - _usage_start)
                    return result  # type: ignore
                except asyncio.TimeoutError as e:
                    self._emit("error", trace_id=trace_id, model=resolved, data={"error": "timeout", "timeout_s": self.timeout_s, "provider": str(self.provider_type.value)})
                    _record_fail("timeout", time.perf_counter() - _usage_start)
                    raise TimeoutError(f"LLM completion timeout after {self.timeout_s}s") from e
                except Exception as e:
                    self._emit("error", trace_id=trace_id, model=resolved, data={"error": str(e), "provider": str(self.provider_type.value)})
                    _record_fail(str(e), time.perf_counter() - _usage_start)
                    raise
            else:
                # provider_type set but instance missing (missing config/creds) — fail-closed in production
                if not _is_mock_allowed():
                    raise RuntimeError(f"LLM provider unavailable — missing transport/config for provider={self.provider_type.value} (fail-closed in production)")
                # non-prod telemetry
                try:
                    from .env_gate import fail_open_telemetry
                    fail_open_telemetry("llm_runtime","provider_missing_fallback_to_litellm", provider=str(self.provider_type.value))
                except Exception:
                    logger.warning("[fail-open] llm_runtime provider %s missing, fallback to litellm", str(self.provider_type.value))

        async def _do() -> dict[str, Any]:
            if self._mock_responses or _load_litellm() is None:
                if self._mock_index < len(self._mock_responses) or _load_litellm() is None:
                    if not _is_mock_allowed() and not self._mock_responses:
                        raise RuntimeError("LLM provider unavailable — mock fallback disabled in production (no litellm, no mock_responses)")
                    return self._next_mock(resolved, messages, tools)
            lm = _load_litellm()
            if lm is None:
                return self._next_mock(resolved, messages, tools)
            ckwargs: dict[str, Any] = dict(kwargs)
            if tools:
                ckwargs["tools"] = tools
            try:
                if hasattr(lm, "acompletion"):
                    resp = await lm.acompletion(model=resolved, messages=messages, **ckwargs)  # type: ignore
                else:
                    resp = await asyncio.to_thread(lm.completion, resolved, messages, **ckwargs)  # type: ignore
                if not isinstance(resp, dict):
                    try:
                        resp = resp.model_dump()  # type: ignore
                    except Exception:
                        resp = dict(resp)  # type: ignore
                return resp  # type: ignore
            except Exception as e:
                raise e

        try:
            result = await _with_timeout(
                _with_retry(
                    _do,
                    max_retries=self.max_retries,
                    backoff_s=self.retry_backoff_s,
                    observability_hook=self.observability_hook,
                    trace_id=trace_id,
                    audit_log=self.audit_log,
                    circuit_breaker=_get_circuit_breaker(f"litellm:{resolved}"),
                ),
                timeout_s=self.timeout_s,
            )
            self._emit("model_response", trace_id=trace_id, model=resolved, data={"request_id": request_id, "finish_reason": result.get("choices", [{}])[0].get("finish_reason", "") if isinstance(result, dict) else ""})
            _record_success(result, time.perf_counter() - _usage_start)
            return result  # type: ignore
        except asyncio.TimeoutError as e:
            self._emit("error", trace_id=trace_id, model=resolved, data={"error": "timeout", "timeout_s": self.timeout_s})
            _record_fail("timeout", time.perf_counter() - _usage_start)
            raise TimeoutError(f"LLM completion timeout after {self.timeout_s}s") from e
        except Exception as e:
            self._emit("error", trace_id=trace_id, model=resolved, data={"error": str(e)})
            _record_fail(str(e), time.perf_counter() - _usage_start)
            raise

    # -- core completion (non-stream) with output_type -------------------

    async def completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        trace_id: str = "",
        request_id: str = "",
        output_type: type[BaseModel] | None = None,
        oaos_context: OAOSContext | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        resolved = self.resolve_model(model)
        if oaos_context is not None and not trace_id:
            trace_id = oaos_context.trace_id
        if stream:
            chunks: list[dict[str, Any]] = []
            async for ch in self.completion_stream(messages, model=model, tools=tools, trace_id=trace_id, request_id=request_id, oaos_context=oaos_context, output_type=output_type, **kwargs):  # type: ignore
                chunks.append(ch)
            content_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for ch in chunks:
                delta = ch.get("choices", [{}])[0].get("delta", {})
                if "content" in delta and delta["content"]:
                    content_parts.append(str(delta["content"]))
                if "tool_calls" in delta and delta["tool_calls"]:
                    tool_calls.extend(delta["tool_calls"])
            return {
                "id": f"stream-collected-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "model": resolved,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "".join(content_parts), "tool_calls": tool_calls},
                        "finish_reason": "stop" if not tool_calls else "tool_calls",
                    }
                ],
                "usage": {},
                "_stream_chunks": chunks,
            }

        # Non-stream path
        raw = await self._raw_completion(messages, model=model, tools=tools, trace_id=trace_id, request_id=request_id, oaos_context=oaos_context, **kwargs)

        if output_type is not None:
            # Validate with retry
            validated_resp, parsed = await _validate_and_retry_output(
                self, messages, raw, output_type, trace_id, request_id, tools, kwargs, max_retries=2
            )
            return validated_resp
        return raw

    # -- streaming -------------------------------------------------------

    async def completion_stream(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        trace_id: str = "",
        request_id: str = "",
        oaos_context: OAOSContext | None = None,
        output_type: type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        resolved = self.resolve_model(model)
        if oaos_context is not None and not trace_id:
            trace_id = oaos_context.trace_id
        self._emit("model_request", trace_id=trace_id, model=resolved, data={"request_id": request_id, "stream": True, "provider": str(self.provider_type.value) if self.provider_type else "litellm", "runtime_mode": str(self.runtime_mode.value)})

        # Hermes mode — stream via mock or hermes API then chunk
        if self.runtime_mode == RuntimeMode.HERMES:
            # mock queue first
            if self._mock_responses and self._mock_index < len(self._mock_responses):
                mock = self._next_mock(resolved, messages, tools)
            else:
                # delegate to hermes single completion then chunk it
                mock = await self._hermes_completion(messages, model=model, tools=tools, trace_id=trace_id, request_id=request_id, oaos_context=oaos_context, **kwargs)
            content = ""
            try:
                content = str(mock.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
                tcs = mock.get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
                if tcs:
                    yield {"id": mock.get("id", ""), "object": "chat.completion.chunk", "model": resolved, "choices": [{"index": 0, "delta": {"tool_calls": tcs}, "finish_reason": None}]}
                    yield {"id": mock.get("id", ""), "object": "chat.completion.chunk", "model": resolved, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
                    self._emit("model_response", trace_id=trace_id, model=resolved, data={"stream": True, "runtime_mode": "hermes", "tool_calls": True})
                    return
            except Exception:
                content = ""
            for ch in _mock_stream_chunks(resolved, content or "mock stream response"):
                yield ch
                await asyncio.sleep(0)
            self._emit("model_response", trace_id=trace_id, model=resolved, data={"stream": True, "runtime_mode": "hermes"})
            return

        # Provider dispatch for streaming — provider call then chunk
        if self.provider_type is not None:
            prov_instance = self._get_provider_instance()
            if prov_instance is not None:
                if self._mock_responses and self._mock_index < len(self._mock_responses):
                    mock = self._next_mock(resolved, messages, tools)
                else:
                    try:
                        mock = await prov_instance.call(messages, model=resolved, tools=tools, trace_id=trace_id, request_id=request_id, **kwargs)
                    except TypeError:
                        mock = await prov_instance.call(messages, model=resolved, tools=tools, **kwargs)
                    except Exception:
                        mock = _mock_completion_response(resolved, messages, tools=tools)
                content = ""
                try:
                    content = str(mock.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
                    tcs = mock.get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
                    if tcs:
                        yield {"id": mock.get("id", ""), "object": "chat.completion.chunk", "model": resolved, "choices": [{"index": 0, "delta": {"tool_calls": tcs}, "finish_reason": None}]}
                        yield {"id": mock.get("id", ""), "object": "chat.completion.chunk", "model": resolved, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
                        self._emit("model_response", trace_id=trace_id, model=resolved, data={"stream": True, "provider": str(self.provider_type.value), "tool_calls": True})
                        return
                except Exception:
                    content = ""
                for ch in _mock_stream_chunks(resolved, content or "mock stream response"):
                    yield ch
                    await asyncio.sleep(0)
                self._emit("model_response", trace_id=trace_id, model=resolved, data={"stream": True, "provider": str(self.provider_type.value)})
                return

        lm = _load_litellm()
        if self._mock_responses or lm is None:
            mock = self._next_mock(resolved, messages, tools)
            content = ""
            try:
                content = str(mock.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
                tcs = mock.get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
                if tcs:
                    yield {"id": mock.get("id", ""), "object": "chat.completion.chunk", "model": resolved, "choices": [{"index": 0, "delta": {"tool_calls": tcs}, "finish_reason": None}]}
                    yield {"id": mock.get("id", ""), "object": "chat.completion.chunk", "model": resolved, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
                    self._emit("model_response", trace_id=trace_id, model=resolved, data={"stream": True, "tool_calls": True})
                    return
            except Exception:
                content = ""
            for ch in _mock_stream_chunks(resolved, content or "mock stream response"):
                yield ch
                await asyncio.sleep(0)
            self._emit("model_response", trace_id=trace_id, model=resolved, data={"stream": True})
            return

        ckwargs: dict[str, Any] = dict(kwargs)
        if tools:
            ckwargs["tools"] = tools
        try:
            stream_gen = await lm.acompletion(model=resolved, messages=messages, stream=True, **ckwargs)  # type: ignore
            async for chunk in stream_gen:  # type: ignore
                if not isinstance(chunk, dict):
                    try:
                        chunk = chunk.model_dump()  # type: ignore
                    except Exception:
                        chunk = dict(chunk)  # type: ignore
                yield chunk  # type: ignore
            self._emit("model_response", trace_id=trace_id, model=resolved, data={"stream": True})
        except Exception as e:
            self._emit("error", trace_id=trace_id, model=resolved, data={"error": str(e), "stream": True})
            raise


# ---------------------------------------------------------------------------
# StructuredToolLoop — with OAOSContext injection + ToolOutputLimits
# ---------------------------------------------------------------------------

GatewayCallable = Any


def _extract_tool_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        choices = response.get("choices") or []
        if not choices:
            return []
        msg = choices[0].get("message") or {}
        tcs = msg.get("tool_calls") or []
        out: list[dict[str, Any]] = []
        for tc in tcs:
            if isinstance(tc, dict):
                out.append(tc)
            else:
                try:
                    out.append(dict(tc))  # type: ignore
                except Exception:
                    continue
        return out
    except Exception:
        return []


def _tool_result_message(tool_call_id: str, tool_name: str, result: Any, limits: ToolOutputLimits | None = None, json_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    content: str
    if isinstance(result, str):
        content = result
    else:
        try:
            content = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            content = str(result)
    # Apply limits
    if limits is not None:
        limited, should_retry, err = limits.apply(content, json_schema=json_schema)
        # err indicates schema violation; we annotate so loop can retry
        if should_retry and err:
            content = limited + f"\n[schema_error: {err}]"
        else:
            content = limited
    return {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": content}


async def _call_gateway(
    gateway: GatewayCallable,
    tool_name: str,
    arguments: dict[str, Any],
    trace_id: str,
    session_id: str = "",
    oaos_context: OAOSContext | None = None,
    limits: ToolOutputLimits | None = None,
    tool_json_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not trace_id:
        raise ValueError("trace_id is required for gateway.call (§16A Zero-Bypass)")

    # Resolve callable with OAOSContext injection
    fn: Callable[..., Any] | None = None
    if hasattr(gateway, "call"):
        fn = getattr(gateway, "call")
    elif hasattr(gateway, "execute"):
        fn = getattr(gateway, "execute")
    elif callable(gateway):
        fn = gateway  # type: ignore
    else:
        raise AttributeError("gateway must have .call or .execute or be callable")

    # Try injection for gateway itself
    prefix_args: tuple[Any, ...] = ()
    call_args = arguments
    if oaos_context is not None:
        try:
            sig = inspect.signature(fn)  # type: ignore
            # Check if first param expects OAOSContext
            params = list(sig.parameters.values())
            if params and params[0].annotation is OAOSContext or (isinstance(params[0].annotation, str) and "OAOSContext" in str(params[0].annotation)) or params[0].name.lower() in ("ctx", "oaos_context", "context"):
                prefix_args = (oaos_context,)
        except Exception:
            pass

    kwargs: dict[str, Any] = {"trace_id": trace_id}
    if session_id:
        kwargs["session_id"] = session_id
    if oaos_context is not None:
        # also propagate vault_path / policy via kwargs if gateway accepts them
        kwargs["vault_path"] = oaos_context.vault_path
        if oaos_context.policy is not None:
            kwargs["policy"] = oaos_context.policy

    # Attempt gateway call with retry for ToolOutputLimits schema violation
    max_attempts = (limits.max_retries + 1) if limits else 1
    last_result: Any = None
    for attempt in range(max_attempts):
        try:
            # Prefer signature: call(tool, args, trace_id=..., session_id=..., vault_path=...)
            if prefix_args:
                if asyncio.iscoroutinefunction(fn):
                    res = await fn(*prefix_args, tool_name, call_args, **kwargs)  # type: ignore
                else:
                    tmp = fn(*prefix_args, tool_name, call_args, **kwargs)  # type: ignore
                    res = await tmp if asyncio.iscoroutine(tmp) else tmp  # type: ignore
            else:
                if asyncio.iscoroutinefunction(fn):
                    res = await fn(tool_name, call_args, **kwargs)  # type: ignore
                else:
                    tmp = fn(tool_name, call_args, **kwargs)  # type: ignore
                    res = await tmp if asyncio.iscoroutine(tmp) else tmp  # type: ignore
            last_result = res
            # Check output limits schema if needed — if violation, retry gateway call
            if limits is not None and tool_json_schema is not None and limits.json_schema_check:
                # Normalize to string for check
                content_for_check = res.get("content") if isinstance(res, dict) and "content" in res else res
                _, should_retry, err = limits.apply(content_for_check if isinstance(content_for_check, str) else json.dumps(content_for_check, ensure_ascii=False, default=str) if isinstance(content_for_check, dict) else str(content_for_check), json_schema=tool_json_schema)
                if should_retry and attempt < max_attempts - 1:
                    # Inject correction arg and retry
                    call_args = dict(call_args)
                    call_args["_correction"] = f"Previous tool output failed schema check: {err}. Please return valid JSON."
                    continue
            return last_result if isinstance(last_result, dict) else {"result": last_result}
        except TypeError:
            # Fallback: call(dict payload, trace_id)
            try:
                payload = {"tool": tool_name, "args": call_args, "arguments": call_args, "trace_id": trace_id}
                if prefix_args:
                    if asyncio.iscoroutinefunction(fn):
                        res2 = await fn(*prefix_args, payload, **kwargs)  # type: ignore
                    else:
                        tmp2 = fn(*prefix_args, payload, **kwargs)  # type: ignore
                        res2 = await tmp2 if asyncio.iscoroutine(tmp2) else tmp2  # type: ignore
                else:
                    if asyncio.iscoroutinefunction(fn):
                        res2 = await fn(payload, **kwargs)  # type: ignore
                    else:
                        tmp2 = fn(payload, **kwargs)  # type: ignore
                        res2 = await tmp2 if asyncio.iscoroutine(tmp2) else tmp2  # type: ignore
                last_result = res2
                return last_result if isinstance(last_result, dict) else {"result": last_result}
            except Exception:
                raise
        except Exception:
            raise
    return last_result if isinstance(last_result, dict) else {"result": last_result}


class StructuredToolLoop:
    """Structured tool loop — §16.1 / §16C.3 with OAOSContext, output_type, limits."""

    def __init__(
        self,
        llm: LLMProviderAdapter,
        gateway: GatewayCallable,
        max_steps: int = 10,
        observability_hook: AuditHook | None = None,
        audit_log: AuditLogStub | None = None,
        timeout_s: float = 120.0,
        tool_output_limits: ToolOutputLimits | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        self.llm = llm
        self.gateway = gateway
        self.max_steps = max_steps
        self.observability_hook = observability_hook
        self.audit_log = audit_log or default_audit_log
        self.timeout_s = timeout_s
        self.tool_output_limits = tool_output_limits or default_tool_limits

    def _emit(self, event_type: str, trace_id: str, data: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"event_type": event_type, "trace_id": trace_id, "data": data or {}}
        if self.audit_log is not None:
            try:
                self.audit_log.emit(payload)
            except Exception:
                pass
        if self.observability_hook is not None:
            try:
                self.observability_hook(payload)
            except Exception:
                pass

    async def run(
        self,
        messages: list[dict[str, Any]],
        trace_id: str,
        tools: list[dict[str, Any]] | None = None,
        session_id: str = "",
        request_id: str = "",
        oaos_context: OAOSContext | dict[str, Any] | None = None,
        output_type: type[BaseModel] | None = None,
        tool_output_limits: ToolOutputLimits | None = None,
        **llm_kwargs: Any,
    ) -> dict[str, Any]:
        if not trace_id and oaos_context is not None:
            if isinstance(oaos_context, dict):
                trace_id = str(oaos_context.get("trace_id", ""))
            else:
                trace_id = str(getattr(oaos_context, "trace_id", ""))
        if not trace_id:
            raise ValueError("trace_id is required (§16A)")

        ctx = _ensure_context(oaos_context, trace_id=trace_id)
        limits = tool_output_limits or self.tool_output_limits

        history: list[dict[str, Any]] = [dict(m) for m in messages]
        steps: int = 0
        terminated: str = "unknown"
        last_response: dict[str, Any] | None = None

        self._emit("tool_loop_start", trace_id, {"session_id": session_id, "max_steps": self.max_steps, "request_id": request_id})

        try:
            async def _loop() -> dict[str, Any]:
                nonlocal steps, terminated, last_response, history
                for step in range(1, self.max_steps + 1):
                    steps = step
                    self._emit("tool_loop_step", trace_id, {"step": step, "history_len": len(history)})
                    try:
                        resp = await self.llm.completion(
                            history, tools=tools, trace_id=trace_id, request_id=request_id or f"req-{step}", oaos_context=ctx, **llm_kwargs
                        )
                    except Exception as e:
                        self._emit("error", trace_id, {"step": step, "error": str(e)})
                        terminated = "error"
                        last_response = {"error": str(e)}
                        break

                    last_response = resp
                    # Handle output_type validation at loop level if llm didn't fully validate (mock path)
                    if output_type is not None and resp.get("_parsed_output") is None and resp.get("_output_type_error"):
                        # LLM validation failed after retries — expose error and terminate as error
                        terminated = "output_type_validation_failed"
                        self._emit("output_type_validation_failed", trace_id, {"step": step, "error": resp.get("_output_type_error")})
                        # Keep history for audit
                        history.append(
                            {
                                "role": "assistant",
                                "content": (resp.get("choices", [{}])[0].get("message", {}).get("content") or ""),
                                "tool_calls": _extract_tool_calls(resp),
                                "_raw": resp,
                                "_output_type_error": resp.get("_output_type_error"),
                            }
                        )
                        break
                    # If output_type expects no tool calls, and we have parsed output, we can terminate early
                    if output_type is not None and resp.get("_parsed_output") is not None:
                        history.append(
                            {
                                "role": "assistant",
                                "content": (resp.get("choices", [{}])[0].get("message", {}).get("content") or ""),
                                "tool_calls": _extract_tool_calls(resp),
                                "_raw": resp,
                                "_parsed_output": resp.get("_parsed_output"),
                            }
                        )
                        terminated = "done"
                        self._emit("tool_loop_done", trace_id, {"step": step, "reason": "output_type_validated"})
                        break

                    history.append(
                        {
                            "role": "assistant",
                            "content": (resp.get("choices", [{}])[0].get("message", {}).get("content") or ""),
                            "tool_calls": _extract_tool_calls(resp),
                            "_raw": resp,
                        }
                    )
                    tcs = _extract_tool_calls(resp)
                    if not tcs:
                        terminated = "done"
                        self._emit("tool_loop_done", trace_id, {"step": step, "reason": "no_tool_calls"})
                        break

                    self._emit("tool_request", trace_id, {"step": step, "tool_calls": [{"name": tc.get("function", {}).get("name") or tc.get("name"), "id": tc.get("id")} for tc in tcs]})

                    for tc in tcs:
                        func = tc.get("function") or {}
                        tool_name = str(func.get("name") or tc.get("name") or "")
                        raw_args = func.get("arguments") or tc.get("arguments") or {}
                        if isinstance(raw_args, str):
                            try:
                                arguments = json.loads(raw_args) if raw_args.strip() else {}
                            except json.JSONDecodeError:
                                arguments = {"_raw": raw_args}
                        elif isinstance(raw_args, dict):
                            arguments = raw_args
                        else:
                            arguments = {}
                        tc_id = str(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}")

                        # Resolve JSON schema for tool if provided in tools list
                        tool_schema: dict[str, Any] | None = None
                        if tools:
                            for t in tools:
                                fn_def = t.get("function") or t
                                if fn_def.get("name") == tool_name:
                                    tool_schema = fn_def.get("parameters") or fn_def.get("json_schema")
                                    break

                        try:
                            gw_result = await _with_timeout(
                                _call_gateway(self.gateway, tool_name, arguments, trace_id=trace_id, session_id=session_id, oaos_context=ctx, limits=limits, tool_json_schema=tool_schema),
                                timeout_s=self.llm.timeout_s,
                            )
                            self._emit("tool_result", trace_id, {"step": step, "tool": tool_name, "tool_call_id": tc_id})
                        except asyncio.TimeoutError:
                            gw_result = {"error": "gateway timeout", "tool": tool_name}
                            self._emit("error", trace_id, {"step": step, "tool": tool_name, "error": "gateway timeout"})
                        except Exception as e:
                            gw_result = {"error": str(e), "tool": tool_name}
                            self._emit("error", trace_id, {"step": step, "tool": tool_name, "error": str(e)})

                        history.append(_tool_result_message(tc_id, tool_name, gw_result, limits=limits, json_schema=tool_schema))

                    if step >= self.max_steps:
                        terminated = "max_steps"
                        self._emit("tool_loop_done", trace_id, {"step": step, "reason": "max_steps"})
                        break
                else:
                    terminated = "max_steps"
                return {"messages": history, "steps": steps, "terminated": terminated, "last_response": last_response}

            result = await _with_timeout(_loop(), timeout_s=self.timeout_s)
            # If output_type requested, surface parsed model at top level for convenience
            if output_type is not None and result.get("last_response") and isinstance(result["last_response"], dict):
                pr = result["last_response"].get("_parsed_output")
                if pr is not None:
                    result["parsed_output"] = pr
                if result["last_response"].get("_output_type_error"):
                    result["output_type_error"] = result["last_response"].get("_output_type_error")
            return result
        except asyncio.TimeoutError:
            self._emit("error", trace_id, {"error": "tool loop timeout", "timeout_s": self.timeout_s})
            return {"messages": history, "steps": steps, "terminated": "timeout", "last_response": last_response, "error": "timeout"}
        finally:
            self._emit("tool_loop_end", trace_id, {"steps": steps, "terminated": terminated})


# ---------------------------------------------------------------------------
# Wired LLMRuntime — Session + Streaming + MCP with OAOSContext/limits/output_type
# ---------------------------------------------------------------------------

class LLMRuntime:
    """Wires session + streaming + MCP client — §16C LLM Runtime.

    Enhancements:
      - OAOSContext injected into every tool/stream
      - ToolOutputLimits enforced on MCP results
      - output_type delegated to LLMProviderAdapter when litellm used
    """

    def __init__(
        self,
        session_manager: SessionManager | None = None,
        streaming_engine: StreamingEngine | None = None,
        mcp_client: MCPClient | None = None,
        model: str | None = None,
        gateway_url: str | None = None,
        tool_output_limits: ToolOutputLimits | None = None,
    ) -> None:
        self.sessions = session_manager or SessionManager()
        self.streaming = streaming_engine or StreamingEngine()
        self.mcp = mcp_client or MCPClient(gateway_url=gateway_url or os.getenv("OAOS_EG_URL"))
        self.model = model or os.getenv("OAOS_LLM_MODEL") or "mock"
        self.tool_output_limits = tool_output_limits or default_tool_limits
        # Lightweight provider for output_type path
        self._provider = LLMProviderAdapter(model=self.model, tool_output_limits=self.tool_output_limits)

    # ── Session delegation (§16C.1) ──
    def create_session(self, tenant_id: str, agent_id: str, user_id: str = "", **kwargs: Any) -> dict[str, Any]:
        return self.sessions.create(tenant_id=tenant_id, agent_id=agent_id, user_id=user_id, **kwargs)

    def resume_session(self, session_id: str, tenant_id: str, agent_id: str) -> dict[str, Any]:
        return self.sessions.resume(session_id, tenant_id, agent_id)

    def cancel_session(self, session_id: str, tenant_id: str, agent_id: str) -> dict[str, Any]:
        return self.sessions.cancel(session_id, tenant_id, agent_id)

    def get_session_state(self, session_id: str, tenant_id: str, agent_id: str) -> dict[str, Any]:
        return self.sessions.get_state(session_id, tenant_id, agent_id)

    def get_oaos_context(self, session_id: str, tenant_id: str, agent_id: str, policy: Any | None = None) -> OAOSContext:
        return self.sessions.get_oaos_context(session_id, tenant_id, agent_id, policy=policy)

    # compatibility aliases
    create = create_session
    resume = resume_session
    cancel = cancel_session
    get_state = get_session_state

    async def acreate_session(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return self.create_session(*a, **kw)

    async def aresume_session(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return self.resume_session(*a, **kw)

    async def acancel_session(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return self.cancel_session(*a, **kw)

    async def aget_session_state(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return self.get_session_state(*a, **kw)

    # ── Streaming (§16C.2) ──
    async def stream_prompt(
        self,
        session_id: str,
        tenant_id: str,
        agent_id: str,
        prompt: str,
        oaos_context: OAOSContext | dict[str, Any] | None = None,
        output_type: type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Validate session isolation then yield streaming events.

        OAOSContext is built from session if not provided and injected into
        streaming and MCP tool calls.
        output_type: if provided, LLM response is validated; on failure a
        final error event is yielded.
        """
        try:
            sess = self.sessions.get_state(session_id, tenant_id, agent_id)
        except Exception as e:
            yield {"type": "error", "data": {"reason": str(e)}, "session_id": session_id}
            yield {"type": "completion", "data": {"session_id": session_id, "error": str(e)}, "session_id": session_id}
            return

        ctx = _ensure_context(oaos_context, session=sess, trace_id=str(sess.get("trace_id", "")))

        # If output_type requested and LLM available, try provider validation path
        if output_type is not None:
            # Use provider to validate single turn
            messages = [{"role": "user", "content": prompt}]
            try:
                prov_resp = await self._provider.completion(messages, trace_id=ctx.trace_id, request_id=kwargs.get("request_id", ""), output_type=output_type, oaos_context=ctx)
                parsed = prov_resp.get("_parsed_output")
                if parsed is not None:
                    # yield as tool-like completion with validated output
                    yield {"type": "text", "data": {"text": _extract_content(prov_resp)}, "trace_id": ctx.trace_id, "session_id": session_id}
                    yield {"type": "completion", "data": {"session_id": session_id, "prompt": prompt, "output_type": output_type.__name__, "validated": True, "parsed": parsed.model_dump() if hasattr(parsed, "model_dump") else str(parsed)}, "trace_id": ctx.trace_id, "session_id": session_id}
                    return
                else:
                    err = prov_resp.get("_output_type_error", "validation failed")
                    yield {"type": "error", "data": {"reason": err, "output_type": output_type.__name__}, "trace_id": ctx.trace_id, "session_id": session_id}
                    yield {"type": "completion", "data": {"session_id": session_id, "error": err}, "trace_id": ctx.trace_id, "session_id": session_id}
                    return
            except Exception as e:
                yield {"type": "error", "data": {"reason": str(e)}, "trace_id": ctx.trace_id, "session_id": session_id}
                yield {"type": "completion", "data": {"session_id": session_id, "error": str(e)}, "trace_id": ctx.trace_id, "session_id": session_id}
                return

        llm_chunks = await self._try_llm(prompt, sess, oaos_context=ctx)
        if llm_chunks is not None:
            async for ev in self.streaming.stream(prompt=prompt, session=sess, chunks=llm_chunks, oaos_context=ctx):
                if ev.get("type") == "tool":
                    tool = ev.get("data", {}).get("tool")
                    args = ev.get("data", {}).get("arguments", {})
                    if tool:
                        try:
                            # Inject OAOSContext via _call style — use mcp with context
                            result = await self.mcp.call_tool(tool, arguments=args, context=ctx.to_dict())
                            # Apply ToolOutputLimits before yielding
                            result_str = json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result
                            limited, _, _ = self.tool_output_limits.apply(result_str)
                            # store limited string as result for token safety
                            try:
                                # try to keep structured if not truncated too much
                                ev["data"]["result"] = json.loads(limited) if limited.strip().startswith("{") else limited
                            except Exception:
                                ev["data"]["result"] = limited
                        except Exception as ex:
                            ev["data"]["result"] = {"error": str(ex)}
                yield ev
            return

        # Mock path
        async for ev in self.streaming.stream(prompt=prompt, session=sess, oaos_context=ctx, **kwargs):
            yield ev

    def stream_events(self, session: Any, **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        if isinstance(session, dict):
            return self.stream_prompt(session.get("session_id", ""), session.get("tenant_id", ""), session.get("agent_id", ""), prompt=kwargs.pop("prompt", ""), oaos_context=session.get("oaos_context") or kwargs.pop("oaos_context", None), **kwargs)
        sid = str(getattr(session, "session_id", ""))
        tid = str(getattr(session, "tenant_id", ""))
        aid = str(getattr(session, "agent_id", ""))
        return self.stream_prompt(sid, tid, aid, prompt=kwargs.pop("prompt", ""), **kwargs)

    async def _try_llm(self, prompt: str, session: dict[str, Any], oaos_context: OAOSContext | None = None) -> list[str] | None:
        api_key = os.getenv("OAOS_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("LITELLM_API_KEY")
        if not api_key:
            return None
        try:
            import litellm  # type: ignore

            model = self.model if self.model != "mock" else os.getenv("OAOS_LLM_MODEL") or "gpt-4o-mini"
            # Include OAOSContext in messages as system hint for trace (non-invasive)
            msgs: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
            if oaos_context is not None:
                # Optional system preamble for policy awareness (not required)
                pass
            resp = await litellm.acompletion(model=model, messages=msgs, max_tokens=512)  # type: ignore
            text = ""
            try:
                text = resp.choices[0].message.content or ""  # type: ignore
            except Exception:
                text = str(resp)
            if not text:
                return None
            chunks = [text[i : i + 40] for i in range(0, len(text), 40)]
            return chunks if chunks else None
        except Exception:
            return None

    # ── MCP delegation with OAOSContext + limits (§16C.5) ──
    async def list_tools(self, tenant_id: str | None = None, agent_id: str | None = None, oaos_context: OAOSContext | None = None) -> list[dict[str, Any]]:
        ctx = oaos_context.to_dict() if isinstance(oaos_context, OAOSContext) else (oaos_context or {})
        if tenant_id:
            ctx["tenant_id"] = tenant_id
        if agent_id:
            ctx["agent_id"] = agent_id
        return await self.mcp.list_tools(context=ctx or None)

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None = None, tenant_id: str | None = None, agent_id: str | None = None, session_id: str | None = None, oaos_context: OAOSContext | dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        # Build OAOSContext if not provided
        if oaos_context is None and (tenant_id or agent_id or session_id):
            # Try to derive from session if session_id given
            if session_id and tenant_id and agent_id:
                try:
                    sess = self.sessions.get_state(session_id, tenant_id, agent_id)
                    oaos_context = OAOSContext.from_session(sess)
                except Exception:
                    oaos_context = OAOSContext(tenant_id=tenant_id or "", agent_id=agent_id or "", session_id=session_id or "", trace_id=kwargs.get("trace_id", ""))
            else:
                oaos_context = OAOSContext(tenant_id=tenant_id or "", agent_id=agent_id or "", session_id=session_id or "", trace_id=kwargs.get("trace_id", ""))
        ctx_dict: dict[str, Any] | None = None
        if isinstance(oaos_context, OAOSContext):
            ctx_dict = oaos_context.to_dict()
        elif isinstance(oaos_context, dict):
            ctx_dict = oaos_context
        else:
            ctx_dict = {}
            if tenant_id:
                ctx_dict["tenant_id"] = tenant_id
            if agent_id:
                ctx_dict["agent_id"] = agent_id
            if session_id:
                ctx_dict["session_id"] = session_id

        result = await self.mcp.call_tool(tool, arguments=arguments, context=ctx_dict or None, **kwargs)
        # Apply limits to result before returning
        result_str = json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result
        limited, _, _ = self.tool_output_limits.apply(result_str)
        if len(result_str) > self.tool_output_limits.truncate_at:
            # Return truncated string wrapped
            return {"tool": tool, "result": limited, "truncated": True, "original_length": len(result_str)}
        return result

    async def health_check(self) -> dict[str, Any]:
        return {"status": "ok", "runtime": "llm", "model": self.model, "features": ["oaos_context", "output_type", "tool_output_limits"]}


# Default singleton + aliases
default_runtime = LLMRuntime()
LLMRuntimeAdapter = LLMRuntime
AgentLLMRuntime = LLMRuntime
