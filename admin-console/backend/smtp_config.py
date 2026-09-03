"""SMTP config API — outbound-mail connector (admin-console/backend/smtp_config.py).

GET  /v1/smtp/config — read effective config (auth; password only as set-flag)
PUT  /v1/smtp/config — L5 update (admin_settings.smtp_config JSON; password write-only)
POST /v1/smtp/test   — L5 connection check only (connect + STARTTLS + login;
                       NEVER sends mail)

Precedence: DB smtp_config JSON > SMTP_* env > defaults.
NOTE: the mailer reads SMTP_* env at startup. DB values are the console
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
router = APIRouter(prefix="/v1/smtp", tags=["smtp"])

SMTP_KEY = "smtp_config"
DEFAULT_PORT = 587

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
        logger.debug(f"smtp DB engine failed: {e}")
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
            row = conn.execute(text("SELECT value FROM admin_settings WHERE key='smtp_config'")).fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logger.debug(f"smtp DB read failed: {e}")
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
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES ('smtp_config', :v, :now, :by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception:
                pass
            try:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES ('smtp_config', :v, :now, :by)"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception as e2:
                logger.debug(f"smtp DB write fallback failed: {e2}")
                return False
    except Exception as e:
        logger.debug(f"smtp DB write failed: {e}")
        return False


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_port() -> int:
    try:
        return int((os.environ.get("SMTP_PORT") or os.environ.get("OAOS_SMTP_PORT") or str(DEFAULT_PORT)).strip())
    except Exception:
        return DEFAULT_PORT


def _env_config() -> dict:
    return {
        "smtp_host": (os.environ.get("SMTP_HOST") or os.environ.get("OAOS_SMTP_HOST") or "").strip(),
        "smtp_port": _env_port(),
        "smtp_user": (os.environ.get("SMTP_USER") or os.environ.get("OAOS_SMTP_USER") or "").strip(),
        "smtp_password_set": bool((os.environ.get("SMTP_PASSWORD") or os.environ.get("OAOS_SMTP_PASSWORD") or "").strip()),
        "use_starttls": _env_bool("SMTP_STARTTLS", _env_bool("OAOS_SMTP_STARTTLS", True)),
    }


def _load_config() -> tuple[dict, str]:
    global _inmem
    raw = _db_get_raw()
    if raw:
        try:
            data = json.loads(raw)
            env = _env_config()
            cfg = {
                "smtp_host": str(data.get("smtp_host") or env["smtp_host"]),
                "smtp_port": int(data.get("smtp_port") or env["smtp_port"]),
                "smtp_user": str(data.get("smtp_user") or env["smtp_user"]),
                "smtp_password_set": bool(data.get("smtp_password_set", False)) or env["smtp_password_set"],
                "use_starttls": bool(data.get("use_starttls", env["use_starttls"])),
            }
            _inmem = cfg
            return cfg, "db"
        except Exception as e:
            logger.debug(f"smtp parse DB failed: {e}")
    if _inmem is not None:
        return dict(_inmem), "in-memory"
    return _env_config(), "env"


class SmtpUpdateRequest(BaseModel):
    smtp_host: Optional[str] = Field(default=None, max_length=256)
    smtp_port: Optional[int] = Field(default=None, ge=1, le=65535)
    smtp_user: Optional[str] = Field(default=None, max_length=256)
    smtp_password: Optional[str] = Field(default=None, max_length=512)
    use_starttls: Optional[bool] = Field(default=None)

    @field_validator("smtp_host")
    @classmethod
    def check_host(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("smtp_host must not be empty")
        return v

    @field_validator("smtp_password")
    @classmethod
    def check_pass(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("smtp_password must not be empty")
        return v


@router.get("/config")
def smtp_get_config(admin: AdminUser = Depends(get_current_admin)) -> dict:
    cfg, source = _load_config()
    return {**cfg, "source": source, "applied": source == "env",
            "note": "DB values require SMTP_* env update + restart to apply" if source != "env" else "live env values in effect"}


@router.put("/config")
def smtp_put_config(req: SmtpUpdateRequest, admin: AdminUser = Depends(require_l5)) -> dict:
    global _inmem
    cfg, _ = _load_config()
    if req.smtp_host is not None:
        cfg["smtp_host"] = req.smtp_host
    if req.smtp_port is not None:
        cfg["smtp_port"] = req.smtp_port
    if req.smtp_user is not None:
        cfg["smtp_user"] = req.smtp_user.strip()
    if req.use_starttls is not None:
        cfg["use_starttls"] = req.use_starttls
    if req.smtp_password is not None:
        cfg["smtp_password_set"] = True
    raw = json.dumps(cfg)
    ok = _db_set_raw(raw, updated_by=getattr(admin, "email", None))
    if ok:
        _inmem = dict(cfg)
        return {**cfg, "source": "db", "applied": False,
                "note": "saved; update SMTP_* env on the host and restart services to apply"}
    if (os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod")):
        raise HTTPException(status_code=503, detail="SMTP config DB unavailable in production (fail-closed)")
    _inmem = dict(cfg)
    return {**cfg, "source": "in-memory", "applied": False,
            "note": "saved in-memory only (dev); update SMTP_* env and restart to apply"}


@router.post("/test")
def smtp_test(body: dict | None = None, admin: AdminUser = Depends(require_l5)) -> dict:
    """Check SMTP connectivity only: connect + STARTTLS + login. Never sends mail."""
    import smtplib
    cfg, source = _load_config()
    override = ""
    if isinstance(body, dict):
        override = str(body.get("smtp_password") or "").strip()
    password = (override or os.environ.get("SMTP_PASSWORD") or os.environ.get("OAOS_SMTP_PASSWORD") or "")
    host = cfg["smtp_host"]
    port = int(cfg["smtp_port"])
    if not host:
        return {"ok": False, "error": "no SMTP host configured (save one first)", "source": source}
    target = f"{host}:{port}"
    t0 = time.monotonic()
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=8.0)
        else:
            server = smtplib.SMTP(host, port, timeout=8.0)
        try:
            server.ehlo()
            tls = False
            if cfg.get("use_starttls") and port != 465:
                try:
                    server.starttls()
                    server.ehlo()
                    tls = True
                except smtplib.SMTPException as e:
                    return {"ok": False, "target": target, "error": f"STARTTLS failed: {str(e)[:160]}", "source": source}
            login_ok = False
            if cfg.get("smtp_user"):
                if not password:
                    return {"ok": False, "target": target, "starttls": tls,
                            "error": "no SMTP password configured (save one first or pass smtp_password for a one-shot probe)", "source": source}
                server.login(cfg["smtp_user"], password)
                login_ok = True
            ms = round((time.monotonic() - t0) * 1000, 1)
            return {"ok": True, "target": target, "status_code": 250,
                    "starttls": tls, "login": login_ok, "latency_ms": ms,
                    "note": "connection verified; no mail was sent", "source": source}
        finally:
            try:
                server.quit()
            except Exception:
                try:
                    server.close()
                except Exception:
                    pass
    except Exception as e:
        return {"ok": False, "target": target,
                "error": f"{type(e).__name__}: {str(e)[:160]}", "source": source}
