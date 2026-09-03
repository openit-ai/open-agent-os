"""Slack config API — incoming-webhook connector (admin-console/backend/slack_config.py).

GET  /v1/slack/config — read effective config (auth; webhook URL only as set-flag)
PUT  /v1/slack/config — L5 update (admin_settings.slack_config JSON; webhook URL write-only)
POST /v1/slack/test   — L5 live probe (POST test message to the incoming webhook)

Precedence: DB slack_config JSON > SLACK_* env > defaults.
NOTE: the notifier reads SLACK_* env at startup. DB values are the console
source of truth; applying requires env update + restart (applied=false).
Secrets never returned/logged. Independent of MM/Outline modules (own helpers,
own admin_settings key).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

try:
    from .auth import AdminUser, get_current_admin, require_l5
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/slack", tags=["slack"])

SLACK_KEY = "slack_config"
TEST_TEXT = "OAOS admin connection test :white_check_mark:"

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
        logger.debug(f"slack DB engine failed: {e}")
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
            row = conn.execute(text("SELECT value FROM admin_settings WHERE key='slack_config'")).fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logger.debug(f"slack DB read failed: {e}")
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
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES ('slack_config', :v, :now, :by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception:
                pass
            try:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES ('slack_config', :v, :now, :by)"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception as e2:
                logger.debug(f"slack DB write fallback failed: {e2}")
                return False
    except Exception as e:
        logger.debug(f"slack DB write failed: {e}")
        return False


def _env_webhook() -> str:
    return (os.environ.get("SLACK_WEBHOOK_URL") or os.environ.get("SLACK_INCOMING_WEBHOOK_URL")
            or os.environ.get("OAOS_SLACK_WEBHOOK_URL") or "").strip()


def _env_config() -> dict:
    return {
        "webhook_url_set": bool(_env_webhook()),
        "channel": (os.environ.get("SLACK_CHANNEL") or os.environ.get("OAOS_SLACK_CHANNEL") or "").strip(),
    }


def _load_config() -> tuple[dict, str]:
    global _inmem
    raw = _db_get_raw()
    if raw:
        try:
            data = json.loads(raw)
            env = _env_config()
            cfg = {
                "webhook_url_set": bool(data.get("webhook_url_set", False)) or env["webhook_url_set"],
                "channel": str(data.get("channel") or env["channel"]),
            }
            _inmem = cfg
            return cfg, "db"
        except Exception as e:
            logger.debug(f"slack parse DB failed: {e}")
    if _inmem is not None:
        return dict(_inmem), "in-memory"
    return _env_config(), "env"


class SlackUpdateRequest(BaseModel):
    webhook_url: Optional[str] = Field(default=None, max_length=512)
    channel: Optional[str] = Field(default=None, max_length=128)

    @field_validator("webhook_url")
    @classmethod
    def check_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("webhook_url must not be empty")
        low = v.lower()
        if not (low.startswith("https://")):
            raise ValueError("webhook_url must start with https://")
        if "hooks.slack.com" not in low:
            raise ValueError("webhook_url must be a Slack incoming-webhook URL (hooks.slack.com)")
        return v

    @field_validator("channel")
    @classmethod
    def check_channel(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("channel must not be empty")
        return v


@router.get("/config")
def slack_get_config(admin: AdminUser = Depends(get_current_admin)) -> dict:
    cfg, source = _load_config()
    return {**cfg, "source": source, "applied": source == "env",
            "note": "DB values require SLACK_* env update + restart to apply" if source != "env" else "live env values in effect"}


@router.put("/config")
def slack_put_config(req: SlackUpdateRequest, admin: AdminUser = Depends(require_l5)) -> dict:
    global _inmem
    cfg, _ = _load_config()
    if req.channel is not None:
        cfg["channel"] = req.channel
    if req.webhook_url is not None:
        cfg["webhook_url_set"] = True
    raw = json.dumps(cfg)
    ok = _db_set_raw(raw, updated_by=getattr(admin, "email", None))
    if ok:
        _inmem = dict(cfg)
        return {**cfg, "source": "db", "applied": False,
                "note": "saved; update SLACK_* env on the host and restart services to apply"}
    if (os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod")):
        raise HTTPException(status_code=503, detail="Slack config DB unavailable in production (fail-closed)")
    _inmem = dict(cfg)
    return {**cfg, "source": "in-memory", "applied": False,
            "note": "saved in-memory only (dev); update SLACK_* env and restart to apply"}


@router.post("/test")
def slack_test(body: dict | None = None, admin: AdminUser = Depends(require_l5)) -> dict:
    """Send a test message via the incoming webhook (optional one-shot URL override, never stored)."""
    cfg, source = _load_config()
    override = ""
    if isinstance(body, dict):
        override = str(body.get("webhook_url") or "").strip()
    webhook = override or _env_webhook()
    if not webhook:
        return {"ok": False, "error": "no webhook URL configured (save one first or pass webhook_url for a one-shot probe)", "source": source}
    low = webhook.lower()
    if not (low.startswith("https://") and "hooks.slack.com" in low):
        return {"ok": False, "error": "webhook_url must be a Slack incoming-webhook URL (hooks.slack.com)", "source": source}
    t0 = time.monotonic()
    try:
        import httpx
        payload: dict = {"text": TEST_TEXT}
        if cfg.get("channel"):
            payload["channel"] = cfg["channel"]
        r = httpx.post(webhook, json=payload, timeout=8.0)
        ms = round((time.monotonic() - t0) * 1000, 1)
        if r.status_code == 200 and r.text.strip().lower() == "ok":
            return {"ok": True, "status_code": 200, "latency_ms": ms, "source": source}
        return {"ok": False, "status_code": r.status_code,
                "error": r.text[:200], "latency_ms": ms, "source": source}
    except Exception as e:
        return {"ok": False,
                "error": f"{type(e).__name__}: {str(e)[:160]}", "source": source}
