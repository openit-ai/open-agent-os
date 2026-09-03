"""MCP config API — MCP server registry for the Execution Gateway (admin-console/backend/mcp_config.py).

GET    /v1/mcp/servers        — list registered MCP servers (auth; header values masked)
POST   /v1/mcp/servers        — L5 register {name, transport, url?, command?, args?, headers?}
PUT    /v1/mcp/servers/{name} — L5 update
DELETE /v1/mcp/servers/{name} — L5 delete
POST   /v1/mcp/servers/{name}/test — L5 live probe (http transports: tools/list; stdio: config validation only)

Persists in DB admin_settings.mcp_servers (JSON dict) > in-memory.
Transports: stdio | sse | streamable-http (mirrors execution_gateway/mcp_registry).
Secrets: header values are write-only (masked as "***" on read, never logged).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

try:
    from .auth import AdminUser, get_current_admin, require_l5
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/mcp", tags=["mcp"])

MCP_KEY = "mcp_servers"
ALLOWED_TRANSPORTS = {"stdio", "sse", "streamable-http"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_db_engine = None
_inmem: dict | None = None


def _db_url() -> str | None:
    try:
        try:
            from persistence import get_database_url  # type: ignore
        except ImportError:
            from .persistence import get_database_url  # type: ignore
        url = get_database_url()
        if url and url.strip():
            return url.strip()
    except Exception:
        pass
    url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    return url.strip() if url and url.strip() else None


def _normalize_sync_url(url: str) -> str:
    u = url.strip()
    if u.startswith("postgresql+asyncpg://"):
        u = u.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    elif u.startswith("postgresql://"):
        u = u.replace("postgresql://", "postgresql+psycopg://", 1)
    if u.startswith("sqlite+"):
        u = u.replace("sqlite+", "sqlite", 1)
    return u


def _get_engine():
    global _db_engine
    if _db_engine is not None:
        return _db_engine
    url = _db_url()
    if not url:
        return None
    try:
        from sqlalchemy import create_engine
        sync_url = _normalize_sync_url(url)
        kwargs: dict = {"pool_pre_ping": True}
        if sync_url.startswith("sqlite"):
            kwargs = {}
            if ":memory:" in sync_url:
                kwargs["connect_args"] = {"check_same_thread": False}
        _db_engine = create_engine(sync_url, **kwargs)
        return _db_engine
    except Exception as e:
        logger.debug(f"mcp DB engine failed: {e}")
        return None


def _ensure_table(engine) -> None:
    try:
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS admin_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT, updated_by TEXT, extra TEXT)"))
    except Exception:
        pass


def _db_get_raw() -> str | None:
    try:
        engine = _get_engine()
        if engine is None:
            return None
        _ensure_table(engine)
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(text("SELECT value FROM admin_settings WHERE key='mcp_servers'")).fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logger.debug(f"mcp DB read failed: {e}")
    return None


def _db_set_raw(value_json: str, updated_by: str | None = None) -> bool:
    try:
        engine = _get_engine()
        if engine is None:
            return False
        _ensure_table(engine)
        from sqlalchemy import text
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with engine.begin() as conn:
            try:
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES ('mcp_servers', :v, :now, :by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception:
                pass
            try:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES ('mcp_servers', :v, :now, :by)"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception as e2:
                logger.debug(f"mcp DB write fallback failed: {e2}")
                return False
    except Exception as e:
        logger.debug(f"mcp DB write failed: {e}")
        return False


def _load_servers() -> tuple[dict, str]:
    global _inmem
    raw = _db_get_raw()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                _inmem = data
                return data, "db"
        except Exception as e:
            logger.debug(f"mcp parse DB failed: {e}")
    if _inmem is not None:
        return dict(_inmem), "in-memory"
    return {}, "empty"


def _save_servers(servers: dict, updated_by: str | None) -> tuple[bool, str]:
    global _inmem
    raw = json.dumps(servers)
    if _db_set_raw(raw, updated_by=updated_by):
        _inmem = dict(servers)
        return True, "db"
    if (os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod")):
        return False, "db-unavailable"
    _inmem = dict(servers)
    return True, "in-memory"


def _public_view(servers: dict) -> list[dict]:
    out = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        headers = cfg.get("headers") or {}
        out.append({
            "name": name,
            "transport": cfg.get("transport"),
            "url": cfg.get("url"),
            "command": cfg.get("command"),
            "args": cfg.get("args") or [],
            "headers_set": sorted(headers.keys()) if isinstance(headers, dict) else [],
            "updated_at": cfg.get("updated_at"),
        })
    return sorted(out, key=lambda s: s["name"])


class McpServerUpsert(BaseModel):
    name: str = Field(max_length=64)
    transport: str = Field(max_length=32)
    url: Optional[str] = Field(default=None, max_length=512)
    command: Optional[str] = Field(default=None, max_length=256)
    args: Optional[list[str]] = Field(default=None)
    headers: Optional[dict[str, str]] = Field(default=None)

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not NAME_RE.match(v):
            raise ValueError("name must match ^[a-z0-9][a-z0-9_-]{0,63}$")
        return v

    @field_validator("transport")
    @classmethod
    def check_transport(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in ALLOWED_TRANSPORTS:
            raise ValueError(f"transport must be one of {sorted(ALLOWED_TRANSPORTS)}")
        return v

    @field_validator("url")
    @classmethod
    def check_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().rstrip("/")
        if len(v) > 512:
            raise ValueError("url too long")
        low = v.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v

    def validate_combo(self) -> None:
        if self.transport in ("sse", "streamable-http") and not self.url:
            raise ValueError(f"url required for transport {self.transport}")
        if self.transport == "stdio" and not (self.command or "").strip():
            raise ValueError("command required for transport stdio")
        if self.args is not None:
            if len(self.args) > 32 or any(len(a) > 256 for a in self.args):
                raise ValueError("args too long (max 32 items, 256 chars each)")
        if self.headers is not None:
            if len(self.headers) > 16:
                raise ValueError("headers too many (max 16)")
            for k, val in self.headers.items():
                if len(k) > 128 or len(str(val)) > 512:
                    raise ValueError("header key/value too long")


@router.get("/servers")
def mcp_list_servers(admin: AdminUser = Depends(get_current_admin)) -> dict:
    servers, source = _load_servers()
    return {"servers": _public_view(servers), "source": source, "count": len(servers)}


@router.post("/servers", status_code=201)
def mcp_add_server(req: McpServerUpsert, admin: AdminUser = Depends(require_l5)) -> dict:
    try:
        req.validate_combo()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    servers, _ = _load_servers()
    if req.name in servers:
        raise HTTPException(status_code=409, detail=f"server '{req.name}' already exists (use PUT)")
    from datetime import datetime, timezone
    entry: dict[str, Any] = {
        "transport": req.transport,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": getattr(admin, "email", None),
    }
    if req.url:
        entry["url"] = req.url
    if req.command:
        entry["command"] = req.command.strip()
    if req.args:
        entry["args"] = req.args
    if req.headers:
        entry["headers"] = dict(req.headers)
    servers[req.name] = entry
    ok, persisted = _save_servers(servers, updated_by=getattr(admin, "email", None))
    if not ok:
        raise HTTPException(status_code=503, detail="MCP registry DB unavailable in production (fail-closed)")
    return {"name": req.name, "persisted": persisted, "server": _public_view({req.name: entry})[0]}


@router.put("/servers/{name}")
def mcp_update_server(name: str, req: McpServerUpsert, admin: AdminUser = Depends(require_l5)) -> dict:
    name = (name or "").strip().lower()
    if req.name != name:
        raise HTTPException(status_code=400, detail="path name and body name must match")
    try:
        req.validate_combo()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    servers, _ = _load_servers()
    if name not in servers:
        raise HTTPException(status_code=404, detail=f"server '{name}' not found")
    from datetime import datetime, timezone
    entry: dict[str, Any] = {
        "transport": req.transport,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": getattr(admin, "email", None),
    }
    if req.url:
        entry["url"] = req.url
    if req.command:
        entry["command"] = req.command.strip()
    if req.args:
        entry["args"] = req.args
    if req.headers:
        entry["headers"] = dict(req.headers)
    servers[name] = entry
    ok, persisted = _save_servers(servers, updated_by=getattr(admin, "email", None))
    if not ok:
        raise HTTPException(status_code=503, detail="MCP registry DB unavailable in production (fail-closed)")
    return {"name": name, "persisted": persisted, "server": _public_view({name: entry})[0]}


@router.delete("/servers/{name}")
def mcp_delete_server(name: str, admin: AdminUser = Depends(require_l5)) -> dict:
    name = (name or "").strip().lower()
    servers, _ = _load_servers()
    if name not in servers:
        raise HTTPException(status_code=404, detail=f"server '{name}' not found")
    del servers[name]
    ok, persisted = _save_servers(servers, updated_by=getattr(admin, "email", None))
    if not ok:
        raise HTTPException(status_code=503, detail="MCP registry DB unavailable in production (fail-closed)")
    return {"deleted": name, "persisted": persisted, "count": len(servers)}


@router.post("/servers/{name}/test")
def mcp_test_server(name: str, admin: AdminUser = Depends(require_l5)) -> dict:
    """Live probe. HTTP transports: JSON-RPC tools/list (5s). stdio: config-only validation."""
    name = (name or "").strip().lower()
    servers, source = _load_servers()
    cfg = servers.get(name)
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=404, detail=f"server '{name}' not found")
    transport = cfg.get("transport")
    if transport == "stdio":
        return {"name": name, "transport": transport, "ok": None,
                "note": "stdio servers run on the Execution Gateway host; validated config only",
                "source": source}
    url = (cfg.get("url") or "").rstrip("/")
    if not url:
        raise HTTPException(status_code=422, detail="server has no url to probe")
    t0 = time.monotonic()
    try:
        import httpx
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        payload = {"jsonrpc": "2.0", "id": "oaos-probe", "method": "tools/list", "params": {}}
        r = httpx.post(url, json=payload, headers=headers, timeout=5.0)
        ms = round((time.monotonic() - t0) * 1000, 1)
        if r.status_code >= 500:
            return {"name": name, "transport": transport, "ok": False,
                    "status_code": r.status_code, "latency_ms": ms, "source": source}
        tools: list = []
        try:
            data = r.json()
            res = data.get("result", data)
            tools = res.get("tools", []) if isinstance(res, dict) else []
        except Exception:
            pass
        names = [t.get("name") for t in tools if isinstance(t, dict) and t.get("name")][:50]
        return {"name": name, "transport": transport, "ok": r.status_code < 400,
                "status_code": r.status_code, "tool_count": len(names), "tools": names,
                "latency_ms": ms, "source": source}
    except Exception as e:
        return {"name": name, "transport": transport, "ok": False,
                "error": f"{type(e).__name__}: {str(e)[:160]}", "source": source}
