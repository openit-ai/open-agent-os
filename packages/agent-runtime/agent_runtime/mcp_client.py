"""MCP client via execution-gateway — §16C.5

Discovery: GET {gateway}/v1/tools  → tool list
Call:      POST {gateway}/v1/execute → tool execution via gateway
No hard deps: httpx optional, falls back to urllib.
"""
from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_GATEWAY = os.getenv("OAOS_EG_URL") or os.getenv("OAOS_EXECUTION_GATEWAY_URL") or "http://localhost:8001"


def _headers_from_context(ctx: dict[str, Any] | None) -> dict[str, str]:
    if not ctx:
        return {}
    h: dict[str, str] = {}
    for k in ("tenant_id", "user_id", "agent_id", "session_id", "trace_id", "request_id", "delegation_id"):
        v = ctx.get(k)
        if v:
            # header names: X-Tenant-Id etc.
            hk = "X-" + "-".join(part.capitalize() for part in k.split("_"))  # tenant_id -> X-Tenant-Id
            # special: delegation_id -> X-Delegation-Id
            if k == "delegation_id":
                hk = "X-Delegation-Id"
            h[hk] = str(v)
    # also support X-Agent-Context JSON fallback
    if ctx and not h:
        h["X-Agent-Context"] = json.dumps(ctx, ensure_ascii=False)
    return h


class MCPClient:
    """Minimal MCP client that proxies through execution-gateway/mcp_registry."""

    def __init__(self, gateway_url: str | None = None, timeout: float = 15.0) -> None:
        self.gateway_url = (gateway_url or DEFAULT_GATEWAY).rstrip("/")
        self.timeout = timeout
        self._cached_tools: list[dict[str, Any]] | None = None

    # ── helpers ──
    def _url(self, path: str) -> str:
        return f"{self.gateway_url}{path}"

    async def _get_json(self, path: str, headers: dict[str, str] | None = None) -> Any:
        try:
            import httpx  # type: ignore

            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(self._url(path), headers=headers or {})
                r.raise_for_status()
                return r.json()
        except ImportError:
            # urllib fallback (sync in thread)
            import asyncio
            import urllib.request

            def _sync() -> Any:
                req = urllib.request.Request(self._url(path), headers=headers or {})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                    return json.loads(resp.read().decode("utf-8"))

            return await asyncio.to_thread(_sync)
        except Exception as e:
            raise RuntimeError(f"GET {path} failed: {e}") from e

    async def _post_json(self, path: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        try:
            import httpx  # type: ignore

            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self._url(path), json=payload, headers=headers or {})
                # 403/429 are business responses — return json even on error
                if r.status_code >= 400:
                    try:
                        return r.json()
                    except Exception:
                        r.raise_for_status()
                return r.json()
        except ImportError:
            import asyncio
            import urllib.request

            def _sync() -> Any:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                hdrs = {"Content-Type": "application/json", **(headers or {})}
                req = urllib.request.Request(self._url(path), data=data, headers=hdrs, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                    return json.loads(resp.read().decode("utf-8"))

            return await asyncio.to_thread(_sync)
        except Exception as e:
            raise RuntimeError(f"POST {path} failed: {e}") from e

    # ── public API ──
    async def list_tools(self, context: dict[str, Any] | None = None, use_cache: bool = False) -> list[dict[str, Any]]:
        """Discovery: list tools via gateway GET /v1/tools.

        Returns list of tool dicts: {name, server, transport, ...}
        Falls back to cached or empty list if gateway unreachable (offline/dev).
        """
        if use_cache and self._cached_tools is not None:
            return self._cached_tools
        try:
            data = await self._get_json("/v1/tools", headers=_headers_from_context(context))
            tools = data.get("tools") if isinstance(data, dict) else data
            if isinstance(tools, list):
                self._cached_tools = tools
                return tools
            return []
        except Exception:
            return list(self._cached_tools or [])

    # alias per spec naming
    async def discover(self, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return await self.list_tools(context=context)

    async def list_resources(self, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        try:
            data = await self._get_json("/v1/tools", headers=_headers_from_context(context))
            if isinstance(data, dict):
                return data.get("resources", [])
            return []
        except Exception:
            return []

    async def call_tool(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        resource: str | None = None,
        action: str | None = None,
        context: dict[str, Any] | None = None,
        capability_token: str | dict | None = None,
    ) -> dict[str, Any]:
        """Call a tool via gateway POST /v1/execute.

        Args:
            tool: MCP tool name (e.g. gmail_search)
            arguments: tool args dict
            resource/action: canonical resource/action (optional — inferred if omitted)
            context: AgentContext dict for header propagation
            capability_token: capability JWT if required for HIGH-risk

        Returns:
            gateway result dict (with trace_id, etc).  Offline fallback returns mock.
        """
        # Infer resource/action from tool if not provided
        _resource = resource or f"tool/{tool}"
        _action = (action or "EXECUTE").upper()
        payload: dict[str, Any] = {
            "tool": tool,
            "resource": _resource,
            "action": _action,
            "args": arguments or {},
        }
        if capability_token is not None:
            payload["capability_token"] = capability_token

        headers = _headers_from_context(context)
        try:
            result = await self._post_json("/v1/execute", payload, headers=headers)
            if isinstance(result, dict):
                return result
            return {"result": result}
        except Exception as e:
            # offline/dev fallback — do not fail callers
            return {"tool": tool, "arguments": arguments or {}, "result": "gateway_unreachable_fallback", "reason": str(e)[:300], "fallback": True}

    # sync wrappers for convenience
    def list_tools_sync(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # cannot run sync wrapper inside running loop — return cached/empty
                return list(self._cached_tools or [])
        except RuntimeError:
            pass
        return asyncio.run(self.list_tools(*args, **kwargs))

    def call_tool_sync(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        import asyncio

        return asyncio.run(self.call_tool(*args, **kwargs))


# Module-level default client
default_client = MCPClient()
