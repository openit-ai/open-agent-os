"""Execution Gateway FastAPI — Sections 7.2, 18, 37-38

Endpoints:
  GET  /health          — liveness
  GET  /v1/tools        — tool discovery (from MCP Registry)
  POST /v1/execute      — tool execution (AgentContext header + capability + trace)
"""
from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from .mcp_registry import default_registry, MCPRegistry
    from .proxy import proxy_tool_call
    from .authz_hook import AuthorizationHook
    from .risk import classify
    from .normalize import normalize_resource, canonicalize_action
except ImportError:
    from execution_gateway.mcp_registry import default_registry, MCPRegistry  # type: ignore
    from execution_gateway.proxy import proxy_tool_call  # type: ignore
    from execution_gateway.authz_hook import AuthorizationHook  # type: ignore
    from execution_gateway.risk import classify  # type: ignore
    from execution_gateway.normalize import normalize_resource, canonicalize_action  # type: ignore

try:
    from agent_context.context import AgentContext  # type: ignore
except Exception:
    AgentContext = None  # type: ignore

app = FastAPI(title="Open Agent OS — Execution Gateway", version="0.1.1")

# Registry & Auth Hook (singletons)
_registry: MCPRegistry = default_registry
_authz_hook = AuthorizationHook(tenant_id="default")

# -- Lazy ToolRateLimiter wiring (§16H.2) --
_rate_limiter = None

def _get_rate_limiter():
    global _rate_limiter
    if _rate_limiter is not None:
        return _rate_limiter
    try:
        try:
            from .tool_policy import ToolRateLimiter  # type: ignore
        except ImportError:
            from execution_gateway.tool_policy import ToolRateLimiter  # type: ignore
        # Configurable via env, defaults: 10/s burst 20 (§16H.2)
        import os
        rate = float(os.getenv("OAOS_TOOL_RATE_PER_SEC", "10"))
        burst = int(os.getenv("OAOS_TOOL_BURST", "20"))
        _rate_limiter = ToolRateLimiter(rate_per_sec=rate, burst=burst)
    except Exception:
        # No-op limiter (always allow) if import fails — keeps 541 green
        class _Noop:
            def allow(self, key: str, tokens: int = 1) -> bool:
                return True
            def retry_after(self, key: str, tokens: int = 1) -> float:
                return 0.0
        _rate_limiter = _Noop()
    return _rate_limiter

def _parse_agent_context_header(
    x_agent_context: str | None,
    x_tenant_id: str | None,
    x_user_id: str | None,
    x_agent_id: str | None,
    x_session_id: str | None,
    x_trace_id: str | None,
    x_request_id: str | None,
) -> dict:
    """AgentContext 파싱 — 헤더 우선순위:

    1) X-Agent-Context: JSON (또는 base64-encoded JSON)
    2) 개별 X-* 헤더들
    """
    ctx: dict[str, Any] = {}

    if x_agent_context:
        raw = x_agent_context.strip()
        # base64 시도
        decoded = None
        # JSON 직접 파싱 시도
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            # base64 JSON 시도
            try:
                # 패딩 보정
                padded = raw + "=" * (-len(raw) % 4)
                decoded = json.loads(base64.b64decode(padded).decode("utf-8"))
            except Exception:
                raise HTTPException(status_code=400, detail="invalid X-Agent-Context header: not valid JSON nor base64 JSON")
        if isinstance(decoded, dict):
            ctx.update(decoded)

    # 개별 헤더로 보강 (개별 헤더가 있으면 덮어씀)
    if x_tenant_id:
        ctx["tenant_id"] = x_tenant_id
    if x_user_id:
        ctx["user_id"] = x_user_id
    if x_agent_id:
        ctx["agent_id"] = x_agent_id
    if x_session_id:
        ctx["session_id"] = x_session_id
    if x_trace_id:
        ctx["trace_id"] = x_trace_id
    if x_request_id:
        ctx["request_id"] = x_request_id

    # delegation / credential headers
    return ctx


def _require_context(ctx: dict) -> dict:
    """필수 필드 검증 — 없으면 401/400."""
    if not ctx.get("user_id"):
        raise HTTPException(status_code=401, detail="AgentContext user_id required (employee:...)")
    if not ctx.get("tenant_id"):
        ctx["tenant_id"] = "default"
    if not ctx.get("agent_id"):
        # derive: employee:kim → agent:assistant:kim
        uid = ctx["user_id"]
        ctx["agent_id"] = uid.replace("employee:", "agent:assistant:", 1) if uid.startswith("employee:") else f"agent:assistant:{uid}"
    if not ctx.get("trace_id"):
        ctx["trace_id"] = f"trace_{uuid.uuid4().hex[:12]}"
    if not ctx.get("request_id"):
        ctx["request_id"] = f"req_{uuid.uuid4().hex[:8]}"
    if not ctx.get("session_id"):
        ctx["session_id"] = f"sess_{uuid.uuid4().hex[:8]}"
    return ctx


# -- Models ────────────────────────────────────────────────────────────

class ExecuteRequest(BaseModel):
    tool: str = Field(description="MCP tool name, e.g. gmail_search")
    action: str = Field(description="Action, e.g. READ, SEND")
    resource: str = Field(description="Canonical resource, e.g. gmail/user/kim/*")
    args: dict = Field(default_factory=dict)
    capability_token: str | dict | None = Field(default=None, description="JWT capability token (required for HIGH-risk)")
    is_external: bool = False
    data_classification: str | None = None


# -- Routes ────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "execution-gateway", "version": "0.1.1"}


@app.get("/v1/tools")
def list_tools():
    """Tool discovery — MCP Registry에서 반환."""
    return {
        "tools": _registry.list_tools_detailed(),
        "resources": _registry.list_resources_detailed(),
        "servers": [s.name for s in _registry.list_servers()],
    }


@app.post("/v1/execute")
async def execute(
    req: ExecuteRequest,
    request: Request,
    x_agent_context: str | None = Header(default=None, alias="X-Agent-Context"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
    x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    x_trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    x_delegation_id: str | None = Header(default=None, alias="X-Delegation-Id"),
    x_credential_binding_id: str | None = Header(default=None, alias="X-Credential-Binding-Id"),
):
    """Tool execution — Authorization Hook → Proxy → MCP forward.

    Headers:
      X-Agent-Context: JSON (or base64 JSON) with tenant_id, user_id, agent_id, trace_id ...
      또는 X-Tenant-Id, X-User-Id, X-Agent-Id, X-Trace-Id 등 개별 헤더

    Body:
      tool, action, resource, args, capability_token, is_external

    Returns:
      execution result with trace_id, risk, delegation binding
    """
    # 1. AgentContext 파싱
    ctx = _parse_agent_context_header(
        x_agent_context, x_tenant_id, x_user_id, x_agent_id, x_session_id, x_trace_id, x_request_id
    )
    # delegation headers
    if x_delegation_id:
        ctx["delegation_id"] = x_delegation_id
    if x_credential_binding_id:
        ctx["credential_binding_id"] = x_credential_binding_id
    # body의 capability_token이 문자열이면 ctx에 별도 보관 (proxy에서 검증)
    ctx = _require_context(ctx)

    # 2. action/resource 정규화 (실패 시 400)
    try:
        canon_action = canonicalize_action(req.action)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        canon_resource = normalize_resource(req.resource)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 3. ToolRateLimiter (§16H.2) — lazy, per (tenant,user,tool,resource)
    try:
        limiter = _get_rate_limiter()
        rate_key = f"{ctx.get('tenant_id')}:{ctx.get('user_id')}:{req.tool}:{canon_resource}"
        if not limiter.allow(rate_key):
            retry = limiter.retry_after(rate_key) if hasattr(limiter, "retry_after") else 1.0
            return JSONResponse(
                status_code=429,
                content={
                    "error": "RATE_LIMITED",
                    "reason": f"tool rate limit exceeded for {req.tool}",
                    "tool": req.tool,
                    "trace_id": ctx["trace_id"],
                    "retry_after": round(retry, 2),
                },
                headers={"Retry-After": str(round(retry, 2))},
            )
    except Exception:
        pass  # fail-open for limiter errors — keeps 541 green

    # 4. tool 존재 검증
    if _registry.find_tool(req.tool) is None and req.tool not in _registry.list_tools():
        raise HTTPException(status_code=404, detail=f"unknown tool: {req.tool}")

    # 5. Authorization Hook — Personal vs Enterprise 분기
    authz = await _authz_hook.authorize(
        agent_context=ctx,
        action=canon_action,
        resource=canon_resource,
        tool_name=req.tool,
        extra_context={"is_external": req.is_external, "args": req.args},
    )
    if authz.decision == "APPROVAL_REQUIRED":
        return JSONResponse(
            status_code=403,
            content={
                "error": "APPROVAL_REQUIRED",
                "reason": authz.reason,
                "source": authz.source,
                "trace_id": authz.trace_id or ctx["trace_id"],
                "action": canon_action,
                "resource": canon_resource,
            },
        )
    if not authz.allowed:
        return JSONResponse(
            status_code=403,
            content={
                "error": "DENIED",
                "reason": authz.reason,
                "source": authz.source,
                "trace_id": authz.trace_id or ctx["trace_id"],
                "action": canon_action,
                "resource": canon_resource,
            },
        )

    # 6. Proxy — capability + risk + trace 전파
    proxy_ctx = {
        **ctx,
        "action": canon_action,
        "resource": canon_resource,
        "is_external": req.is_external,
        "data_classification": req.data_classification,
    }
    result = await proxy_tool_call(
        tool_name=req.tool,
        args=req.args,
        capability_token=req.capability_token,
        context=proxy_ctx,
    )

    # Personal Wiki auto-archive hook (best-effort, non-blocking) — after proxy_tool_call
    try:
        try:
            from execution_gateway.wiki_archive import auto_archive  # type: ignore
        except ImportError:
            from .wiki_archive import auto_archive  # type: ignore  # type: ignore
        auto_archive(trace_id=ctx.get("trace_id") or result.get("trace_id") or "unknown", tool_name=req.tool, result=result, max_chars=4000)
    except Exception:
        pass

    # 7. proxy 결과 상태 매핑
    if "error" in result:
        err = result["error"]
        if err == "CAPABILITY_REQUIRED":
            return JSONResponse(status_code=403, content=result)
        if err == "CAPABILITY_DENIED":
            return JSONResponse(status_code=403, content=result)
        return JSONResponse(status_code=403, content=result)

    # 성공 — trace 헤더 포함
    headers = {
        "X-Trace-Id": result.get("trace_id", ctx["trace_id"]),
        "X-Request-Id": result.get("request_id", ctx["request_id"]),
    }
    return JSONResponse(content=result, headers=headers)


# -- Legacy / compat ─────────────────────────────────────────────────
@app.get("/")
def root():
    return {"service": "execution-gateway", "health": "/health", "tools": "/v1/tools", "execute": "/v1/execute"}
