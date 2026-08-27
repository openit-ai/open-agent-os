"""Hermes concrete runtime adapter — implements AgentRuntimeAdapter.

Migrated from ``control_plane.hermes_adapter`` (thin wrapper) +
``control_plane.acp_adapter.ACPAdapter`` (ACP wire logic) into the
runtime-adapter package as the canonical Hermes backend (Section 16E).

Keeps backward-compatible ``HermesAdapter`` alias.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

import httpx

from .adapter import AgentRuntimeAdapter

# Reuse router logic if available; otherwise inline minimal mapping.
try:
    from control_plane.router import select_worker_pool  # type: ignore[import-untyped]
except Exception:  # pragma: no cover — standalone package without control-plane on path
    _DOMAIN_POOLS = {
        "general": "hermes-general",
        "development": "hermes-dev",
        "finance_hr": "hermes-finance-hr",
        "admin": "hermes-admin",
        "high_risk_ephemeral": "hermes-ephemeral",
    }
    _HIGH_RISK_ACTIONS = {"DEPLOY", "MERGE", "PAY", "DELETE", "EXPORT"}

    def select_worker_pool(security_domain: str, risk_level: str = "LOW", action: str | None = None) -> str:  # type: ignore[no-redef]
        if action in _HIGH_RISK_ACTIONS or risk_level == "HIGH":
            return _DOMAIN_POOLS["high_risk_ephemeral"]
        return _DOMAIN_POOLS.get(security_domain, _DOMAIN_POOLS["general"])


def _headers_for_session(session: Any) -> dict[str, str]:
    """Build AgentContext propagation headers from SessionRecord or dict."""
    if isinstance(session, dict):
        return {
            "X-Tenant-Id": str(session.get("tenant_id", "")),
            "X-User-Id": str(session.get("user_id", "")),
            "X-Agent-Id": str(session.get("agent_id", "")),
            "X-Session-Id": str(session.get("session_id", "")),
            "X-Trace-Id": str(session.get("trace_id", "")),
            "X-Security-Domain": str(session.get("security_domain", "general")),
        }
    # SessionRecord/dataclass
    return {
        "X-Tenant-Id": getattr(session, "tenant_id", ""),
        "X-User-Id": getattr(session, "user_id", ""),
        "X-Agent-Id": getattr(session, "agent_id", ""),
        "X-Session-Id": getattr(session, "session_id", ""),
        "X-Trace-Id": getattr(session, "trace_id", ""),
        "X-Security-Domain": getattr(session, "security_domain", "general"),
    }


def _session_id_of(session: Any) -> str:
    if isinstance(session, dict):
        return str(session.get("session_id", ""))
    return str(getattr(session, "session_id", ""))


class HermesRuntimeAdapter(AgentRuntimeAdapter):
    """Hermes backend for the runtime-adapter contract."""

    def __init__(self, hermes_base_url: str = "http://localhost:8001", timeout_s: float = 30.0):
        self.hermes_base_url = hermes_base_url.rstrip("/")
        self.timeout_s = timeout_s

    # ── Legacy helpers preserved from control_plane.hermes_adapter ──

    def resolve_pool(self, session: Any, action: str | None = None, risk: str = "LOW") -> str:
        sec = session.get("security_domain") if isinstance(session, dict) else getattr(session, "security_domain", "general")
        return select_worker_pool(sec, risk_level=risk, action=action)

    def worker_url(self, pool: str) -> str:  # noqa: ARG002
        # In prod: pool maps to K8s service/VM; in dev: single Hermes instance
        return self.hermes_base_url

    # ── AgentRuntimeAdapter contract ──

    async def create_session(self, session: Any) -> dict[str, Any]:
        sid = _session_id_of(session)
        payload = {
            "session_id": sid,
            "agent_id": getattr(session, "agent_id", session.get("agent_id") if isinstance(session, dict) else ""),
            "user_id": getattr(session, "user_id", session.get("user_id") if isinstance(session, dict) else ""),
            "tenant_id": getattr(session, "tenant_id", session.get("tenant_id") if isinstance(session, dict) else ""),
            "security_domain": getattr(session, "security_domain", session.get("security_domain") if isinstance(session, dict) else "general"),
            "trace_id": getattr(session, "trace_id", session.get("trace_id") if isinstance(session, dict) else ""),
        }
        # Clean empty strings for dict case
        if isinstance(session, dict):
            payload = {k: session.get(k, v) for k, v in payload.items()}
        url = f"{self.hermes_base_url}/acp/sessions"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(url, json=payload, headers=_headers_for_session(session))
                r.raise_for_status()
                return r.json()
        except Exception as e:
            return {"status": "local_fallback", "reason": str(e), "session_id": sid}

    async def send_prompt(self, session: Any, prompt: str, request_id: str) -> dict[str, Any]:
        sid = _session_id_of(session)
        trace = getattr(session, "trace_id", session.get("trace_id") if isinstance(session, dict) else "")
        url = f"{self.hermes_base_url}/acp/sessions/{sid}/prompt"
        payload = {"prompt": prompt, "request_id": request_id, "trace_id": trace}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(url, json=payload, headers=_headers_for_session(session))
                r.raise_for_status()
                return r.json()
        except Exception as e:
            return {"status": "queued_local", "reason": str(e), "request_id": request_id}

    async def stream_events(self, session: Any) -> AsyncGenerator[dict[str, Any], None]:
        sid = _session_id_of(session)
        trace = getattr(session, "trace_id", session.get("trace_id") if isinstance(session, dict) else "")
        url = f"{self.hermes_base_url}/acp/sessions/{sid}/stream"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                async with client.stream("GET", url, headers=_headers_for_session(session)) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(f"stream status {resp.status_code}")
                    async for line in resp.aiter_lines():
                        if not line or line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            data = line[5:].strip()
                            try:
                                yield json.loads(data)
                            except json.JSONDecodeError:
                                yield {"type": "token", "data": {"text": data}}
                    return
        except Exception:
            pass
        # Dev fallback synthetic stream
        for chunk in ["안녕하세요, ", "Personal Agent가 ", "준비되었습니다."]:
            await asyncio.sleep(0.02)
            yield {"type": "token", "data": {"text": chunk}, "trace_id": trace}
        yield {"type": "done", "data": {}, "trace_id": trace}

    async def cancel_session(self, session_id: str, caller_user_id: str | None = None) -> dict[str, Any]:
        url = f"{self.hermes_base_url}/acp/sessions/{session_id}/cancel"
        headers: dict[str, str] = {}
        if caller_user_id:
            headers["X-User-Id"] = caller_user_id
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(url, headers=headers)
                r.raise_for_status()
                return r.json()
        except Exception as e:
            return {"status": "cancelled_local", "reason": str(e), "session_id": session_id}

    async def get_session_state(self, session_id: str, caller_user_id: str | None = None) -> dict[str, Any]:
        url = f"{self.hermes_base_url}/acp/sessions/{session_id}"
        headers: dict[str, str] = {}
        if caller_user_id:
            headers["X-User-Id"] = caller_user_id
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.get(url, headers=headers)
                r.raise_for_status()
                return r.json()
        except Exception as e:
            return {"status": "local", "reason": str(e), "session_id": session_id}

    async def health_check(self) -> dict[str, Any]:
        for path in ("/health", "/acp/health"):
            url = f"{self.hermes_base_url}{path}"
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.get(url)
                    if r.status_code == 200:
                        data = r.json() if "application/json" in r.headers.get("content-type", "") else {"raw": r.text}
                        return {"status": "ok", "url": url, "data": data}
            except Exception:
                continue
        # Hermes not reachable — report degraded but not error for dev
        return {"status": "degraded", "reason": "hermes unreachable", "base_url": self.hermes_base_url}


# Backward-compatible alias — legacy imports expect HermesAdapter
HermesAdapter = HermesRuntimeAdapter
