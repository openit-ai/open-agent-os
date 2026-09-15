"""Admin onboarding progress, readiness, discovery, and test contracts.

Readiness is computed from existing domain configuration and persisted, redacted
health observations. The in-memory observation cache is only a fallback when no
admin database is configured.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import secrets
import smtplib
import socket
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field

try:
    from .auth import AdminUser, get_current_admin, require_l5
    from .config_state import apply_state_fields, public_revision, utc_now_iso
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore
    from config_state import apply_state_fields, public_revision, utc_now_iso  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-readiness"])

_CANDIDATE_TTL_SECONDS = 120
_DEFAULT_CHECK_TTL_SECONDS = 300
_cache_lock = threading.Lock()
_candidate_refs: dict[str, tuple[float, dict[str, Any]]] = {}
_recent_tests: dict[str, tuple[float, dict[str, Any]]] = {}

_SUPPORTED_KINDS = {
    "acp", "control-plane", "mattermost", "slack", "outline", "notion",
    "oauth", "smtp", "mcp",
}

# Product defaults only.  This fixed table is the complete loopback probe
# allowlist; discovery never enumerates ranges or ports.
_DEFAULT_TARGETS: dict[str, tuple[str, ...]] = {
    "control-plane": ("http://127.0.0.1:8100",),
    "acp": ("http://127.0.0.1:8001",),
    "mattermost": ("http://127.0.0.1:8065",),
    "outline": ("http://127.0.0.1:3000",),
}

_NEXT_ACTIONS = {
    "control-plane": {"label_key": "admin.readiness.actions.openHealth", "href": "/operations/health"},
    "execution": {"label_key": "admin.readiness.actions.configureRuntime", "href": "/execution"},
    "acp": {"label_key": "admin.readiness.actions.configureAcp", "href": "/control/acp"},
    "ingress": {"label_key": "admin.readiness.actions.configureIngress", "href": "/connections"},
    "mattermost": {"label_key": "admin.readiness.actions.configureMattermost", "href": "/connections/mattermost"},
    "slack": {"label_key": "admin.readiness.actions.configureSlack", "href": "/connections/slack"},
    "policy": {"label_key": "admin.readiness.actions.configurePolicy", "href": "/control/policy"},
    "environment": {"label_key": "admin.readiness.actions.openHealth", "href": "/operations/health"},
    "mcp": {"label_key": "admin.readiness.actions.configureMcp", "href": "/execution/mcp"},
    "knowledge": {"label_key": "admin.readiness.actions.configureKnowledge", "href": "/knowledge"},
    "notifications": {"label_key": "admin.readiness.actions.configureNotifications", "href": "/connections"},
    "verify": {"label_key": "admin.readiness.actions.reviewProblems", "href": "/"},
}


class TestConnectionRequest(BaseModel):
    candidate_id: str | None = Field(default=None, max_length=128)
    config_revision: str | None = Field(default=None, max_length=128)
    mode: Literal["safe", "write_probe"] = "safe"


class _AdapterFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _domain(name: str):
    for qualified in (f"admin_console.backend.{name}", name):
        mod = sys.modules.get(qualified)
        if mod is not None:
            return mod
    try:
        return importlib.import_module(f"admin_console.backend.{name}")
    except ImportError:
        return importlib.import_module(name)


def _ttl_seconds() -> int:
    try:
        return max(30, min(int(os.environ.get("OAOS_ADMIN_READINESS_TTL_SECONDS", "300")), 3600))
    except (TypeError, ValueError):
        return _DEFAULT_CHECK_TTL_SECONDS


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _fresh(checked_at: Any, now: datetime) -> bool:
    parsed = _parse_time(checked_at)
    return bool(parsed and 0 <= (now - parsed).total_seconds() <= _ttl_seconds())


def _target_display(target: str | None) -> str | None:
    """Return only a non-secret host/service display; never return URL queries."""
    raw = (target or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        # Service-registry names and host:port values only.
        return raw.split("?", 1)[0].split("#", 1)[0][:160]
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        if not host:
            return None
        display = host
        if parsed.port:
            display += f":{parsed.port}"
        return display[:160]
    except (ValueError, TypeError):
        return None


def _redacted_observation(connection_id: str) -> dict[str, Any] | None:
    infra = None
    db_enabled = False
    try:
        infra = _domain("infra")
        db_enabled = infra.readiness_observation_backend_enabled()
    except Exception as exc:  # noqa: BLE001 - in-memory mode can load without infra
        logger.debug("readiness observation backend detection failed: %s", type(exc).__name__)
    if db_enabled:
        try:
            observation = infra.load_readiness_observation(connection_id)
            return dict(observation) if observation else None
        except Exception as exc:  # noqa: BLE001 - DB remains authoritative when configured
            logger.warning("readiness observation load failed: %s", type(exc).__name__)
            return None
    with _cache_lock:
        item = _recent_tests.get(connection_id)
        if not item:
            return None
        expires_at, observation = item
        if expires_at <= time.monotonic():
            _recent_tests.pop(connection_id, None)
            return None
        return dict(observation)


def _record_observation(connection_id: str, observation: dict[str, Any]) -> None:
    """Persist a redacted observation, with a bounded in-memory no-DB fallback."""
    stored = dict(observation)
    previous = _redacted_observation(connection_id)
    if stored.get("ok") is True and stored.get("code") == "OK":
        stored["last_success_at"] = stored.get("checked_at")
    elif previous and previous.get("last_success_at"):
        stored["last_success_at"] = previous["last_success_at"]
    infra = None
    db_enabled = False
    try:
        infra = _domain("infra")
        db_enabled = infra.readiness_observation_backend_enabled()
    except Exception as exc:  # noqa: BLE001 - in-memory mode can load without infra
        logger.debug("readiness observation backend detection failed: %s", type(exc).__name__)
    if db_enabled:
        try:
            infra.persist_readiness_observation(connection_id, stored)
            return
        except Exception as exc:  # noqa: BLE001 - never replace DB state with memory
            logger.warning("readiness observation persistence failed: %s", type(exc).__name__)
            return
    now = time.monotonic()
    with _cache_lock:
        expired = [key for key, (expires, _) in _recent_tests.items() if expires <= now]
        for key in expired:
            _recent_tests.pop(key, None)
        if len(_recent_tests) >= 256:
            oldest = min(_recent_tests, key=lambda key: _recent_tests[key][0])
            _recent_tests.pop(oldest, None)
        _recent_tests[connection_id] = (now + _ttl_seconds(), stored)


def _classify(
    *,
    required: bool,
    configured: bool,
    applied: bool,
    observation: dict[str, Any] | None,
    now: datetime,
) -> tuple[str, str]:
    if not configured:
        return ("failed", "REQUIRED_MISSING") if required else ("warning", "OPTIONAL_UNCONFIGURED")
    if not applied:
        return "warning", "NOT_APPLIED"
    if not observation:
        return "warning", "CHECK_REQUIRED"
    code = str(observation.get("code") or "UNKNOWN")
    freshness_at = (
        observation.get("last_success_at") or observation.get("checked_at")
        if observation.get("ok") is True and code == "OK"
        else observation.get("checked_at")
    )
    if not _fresh(freshness_at, now):
        return "warning", "CHECK_EXPIRED"
    if observation.get("ok") is True and code == "OK":
        return "healthy", "OK"
    if observation.get("status") == "warning" or code == "NOT_APPLIED":
        return "warning", code
    return "failed", code


def _connection(
    connection_id: str,
    kind: str,
    *,
    required: bool,
    configured: bool,
    applied: bool,
    config_revision: str,
    effective_revision: str,
    requires_restart: bool,
    source: str,
    observation: dict[str, Any] | None,
    now: datetime,
    target: str | None = None,
) -> dict[str, Any]:
    state, code = _classify(
        required=required,
        configured=configured,
        applied=applied,
        observation=observation,
        now=now,
    )
    checked_at = (observation or {}).get("checked_at") or now.isoformat()
    item: dict[str, Any] = {
        "id": connection_id,
        "kind": kind,
        "state": state,
        "applied": applied,
        "requires_restart": requires_restart,
        "checked_at": checked_at,
        "config_revision": config_revision,
        "effective_revision": effective_revision,
        "source": source,
        "_required": required,
        "_configured": configured,
        "_code": code,
    }
    latency = (observation or {}).get("latency_ms")
    if latency is not None:
        item["latency_ms"] = latency
    display = _target_display(target or (observation or {}).get("target_display"))
    if display:
        item["target_display"] = display
    return item


def _unavailable_connection(connection_id: str, now: datetime) -> dict[str, Any]:
    revision = public_revision(connection_id, {"available": False})
    return _connection(
        connection_id, connection_id, required=True, configured=False, applied=False,
        config_revision=revision, effective_revision=revision, requires_restart=False,
        source="unavailable", observation=None, now=now,
    )


def _config_fact(name: str, configured: bool, now: datetime) -> dict[str, Any]:
    mod = _domain(f"{name}_config")
    cfg, source = mod._load_config()
    effective = mod._env_config()
    meta = apply_state_fields(name, cfg, source, effective_config=effective)
    return {
        "config": cfg,
        "source": source,
        "configured": configured,
        **meta,
    }


def _database_check(now: datetime) -> dict[str, Any]:
    try:
        setup = _domain("setup")
        engine = setup._get_engine()
        if engine is None:
            raise RuntimeError("database not configured")
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "code": "OK", "checked_at": now.isoformat()}
    except Exception:  # noqa: BLE001 - a failed dependency check becomes a failed fact
        return {"ok": False, "code": "UNREACHABLE", "checked_at": now.isoformat()}


def _control_plane_observation(now: datetime) -> tuple[bool, dict[str, Any] | None, str | None]:
    """Read the existing infra registry/cache without triggering a new probe."""
    try:
        infra = _domain("infra")
        rows: list[Any] = []
        db_rows = infra._db_list_services()
        if db_rows:
            rows.extend(db_rows)
        rows.extend(list(getattr(infra, "_services", {}).values()))
        for row in rows:
            name = getattr(row, "name", None) or (row.get("name") if isinstance(row, dict) else None)
            if name != "control-plane":
                continue
            status = getattr(row, "status", None) or (row.get("status") if isinstance(row, dict) else None)
            status = getattr(status, "value", status)
            checked = getattr(row, "last_check", None) or (row.get("last_check") if isinstance(row, dict) else None)
            host = getattr(row, "host", None) or (row.get("host") if isinstance(row, dict) else None)
            port = getattr(row, "port", None) or (row.get("port") if isinstance(row, dict) else None)
            if status == "healthy":
                normalized_status, code, ok = "healthy", "OK", True
            elif status == "degraded":
                normalized_status, code, ok = "warning", "DEGRADED", False
            elif status in {None, "unknown"}:
                normalized_status, code, ok = "warning", "CHECK_REQUIRED", False
            else:
                normalized_status, code, ok = "failed", "UNREACHABLE", False
            obs = {
                "ok": ok,
                "status": normalized_status,
                "code": code,
                "checked_at": str(checked) if checked else datetime.fromtimestamp(0, UTC).isoformat(),
                "target_display": f"{host}:{port}" if host and port else "control-plane",
                "latency_ms": getattr(row, "latency_ms", None) or (row.get("latency_ms") if isinstance(row, dict) else None),
            }
            return True, obs, obs["target_display"]
    except Exception as exc:  # noqa: BLE001 - registry adapters are optional inputs
        logger.debug("readiness infra read failed: %s", type(exc).__name__)
    # The fixed manifest entry establishes configuration, but not health.
    return True, _redacted_observation("control-plane"), "127.0.0.1:8100"


def _runtime_connection(now: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    mode_mod = _domain("runtime_mode")
    mode = getattr(mode_mod.get_mode(), "value", str(mode_mod.get_mode()))
    supporting: list[dict[str, Any]] = []
    if mode == "llm":
        llm = _domain("llm_providers")
        providers = [p for p in llm._list_all_providers() if getattr(p, "enabled", False)]
        observations = []
        for provider in providers:
            checked = getattr(provider, "last_test_at", None)
            ok = getattr(provider, "last_test_status", None) == "ok"
            observations.append({
                "ok": ok,
                "status": "healthy" if ok else "failed",
                "code": "OK" if ok else "MISCONFIGURED",
                "checked_at": checked.isoformat() if isinstance(checked, datetime) else checked,
                "latency_ms": getattr(provider, "last_test_latency_ms", None),
            })
        observation = next((o for o in observations if o["ok"] and _fresh(o.get("checked_at"), now)), observations[0] if observations else None)
        cfg = {"mode": "llm", "providers": [str(getattr(p, "id", "")) for p in providers]}
        revision = public_revision("execution", cfg)
        execution = _connection(
            "execution", "execution", required=True, configured=bool(providers), applied=True,
            config_revision=revision, effective_revision=revision, requires_restart=False,
            source="llm-providers", observation=observation, now=now,
        )
        return execution, supporting

    acp_mod = _domain("acp_config")
    cfg, source = acp_mod._load_config()
    meta = apply_state_fields("acp", cfg, source, effective_config=acp_mod._env_config())
    configured = bool(cfg.get("acp_enabled") and cfg.get("hermes_base_url"))
    acp = _connection(
        "acp", "acp", required=False, configured=configured, applied=meta["applied"],
        config_revision=meta["config_revision"], effective_revision=meta["effective_revision"],
        requires_restart=meta["requires_restart"], source=source,
        observation=_redacted_observation("acp"), now=now, target=cfg.get("hermes_base_url"),
    )
    supporting.append(acp)
    execution = dict(acp)
    execution.update({"id": "execution", "kind": "execution", "_required": True})
    if not configured:
        execution.update({"state": "failed", "_code": "REQUIRED_MISSING"})
    return execution, supporting


def _ingress_connection(now: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    definitions = (
        ("mattermost", lambda c: bool(c.get("mattermost_url") and c.get("bot_token_set")), "mattermost_url"),
        ("slack", lambda c: bool(c.get("webhook_url_set")), None),
    )
    physical: list[dict[str, Any]] = []
    for name, configured_fn, target_key in definitions:
        mod = _domain(f"{name}_config")
        cfg, source = mod._load_config()
        meta = apply_state_fields(name, cfg, source, effective_config=mod._env_config())
        physical.append(_connection(
            name, name, required=False, configured=configured_fn(cfg), applied=meta["applied"],
            config_revision=meta["config_revision"], effective_revision=meta["effective_revision"],
            requires_restart=meta["requires_restart"], source=source,
            observation=_redacted_observation(name), now=now,
            target=cfg.get(target_key) if target_key else "slack.com",
        ))
    healthy = next((p for p in physical if p["state"] == "healthy"), None)
    configured_items = [p for p in physical if p["_configured"]]
    chosen = healthy or (configured_items[0] if configured_items else physical[0])
    aggregate_cfg = {p["id"]: p["config_revision"] for p in physical}
    revision = public_revision("ingress", aggregate_cfg)
    state = "healthy" if healthy else ("warning" if configured_items else "failed")
    code = "OK" if healthy else (chosen["_code"] if configured_items else "REQUIRED_MISSING")
    ingress = {
        "id": "ingress", "kind": "ingress", "state": state,
        "applied": any(p["applied"] for p in configured_items),
        "requires_restart": bool(configured_items and any(p["requires_restart"] for p in configured_items)),
        "checked_at": chosen["checked_at"], "config_revision": revision,
        "effective_revision": revision if healthy else public_revision("ingress-effective", aggregate_cfg),
        "source": chosen["source"], "_required": True,
        "_configured": bool(configured_items), "_code": code,
    }
    if chosen.get("latency_ms") is not None:
        ingress["latency_ms"] = chosen["latency_ms"]
    return ingress, physical


def _policy_connection(now: datetime) -> tuple[dict[str, Any], dict[str, bool]]:
    auth = _domain("auth")
    policy = _domain("policy")
    admins = auth.list_users()
    has_l5 = any(str(getattr(getattr(u, "role", None), "value", getattr(u, "role", ""))) == "L5" for u in admins)
    active = policy.get_active_published_bundle("default")
    active_exists = active is not None
    valid = False
    if active_exists:
        valid, _ = policy._validate_rules(active.get("rules") or [], allow_remove_mandatory=False)
    configured = has_l5 and active_exists
    ok = configured and valid
    revision = public_revision("policy", {"version": (active or {}).get("version")})
    observation = {
        "ok": ok, "status": "healthy" if ok else "failed",
        "code": "OK" if ok else "MISCONFIGURED", "checked_at": now.isoformat(),
    }
    conn = _connection(
        "policy", "policy", required=True, configured=configured, applied=True,
        config_revision=revision, effective_revision=revision, requires_restart=False,
        source="policy", observation=observation, now=now,
    )
    return conn, {"l5_admin": has_l5, "active_version": active_exists, "valid": valid}


def _optional_facts(now: datetime) -> dict[str, dict[str, Any]]:
    mcp = _domain("mcp_config")
    servers, _ = mcp._load_servers()
    mcp_results = [_redacted_observation(f"mcp:{name}") for name in servers]
    mcp_complete = bool(servers) and all(r and r.get("ok") and _fresh(r.get("checked_at"), now) for r in mcp_results)

    selected_knowledge: list[tuple[str, dict[str, Any], str]] = []
    for name in ("outline", "notion"):
        mod = _domain(f"{name}_config")
        cfg, source = mod._load_config()
        is_configured = bool(cfg.get("api_key_set"))
        if is_configured:
            selected_knowledge.append((name, apply_state_fields(name, cfg, source, effective_config=mod._env_config()), source))
    knowledge_complete = bool(selected_knowledge) and all(
        meta["applied"] and (_redacted_observation(name) or {}).get("ok") is True
        and _fresh((_redacted_observation(name) or {}).get("checked_at"), now)
        for name, meta, _ in selected_knowledge
    )

    notification_names: list[str] = []
    slack = _domain("slack_config")
    slack_cfg, _ = slack._load_config()
    if slack_cfg.get("webhook_url_set"):
        notification_names.append("slack")
    smtp = _domain("smtp_config")
    smtp_cfg, _ = smtp._load_config()
    if smtp_cfg.get("smtp_host"):
        notification_names.append("smtp")
    notifications_complete = any(
        (_redacted_observation(name) or {}).get("ok") is True
        and _fresh((_redacted_observation(name) or {}).get("checked_at"), now)
        for name in notification_names
    )
    return {
        "mcp": {"selected": bool(servers), "complete": mcp_complete},
        "knowledge": {"selected": bool(selected_knowledge), "complete": knowledge_complete},
        "notifications": {"selected": bool(notification_names), "complete": notifications_complete},
    }


def _collect_snapshot(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    backend = {"ok": True, "code": "OK", "checked_at": now.isoformat()}
    database = _database_check(now)
    cp_configured, cp_observation, cp_target = _control_plane_observation(now)
    cp_revision = public_revision("control-plane", {"target": cp_target})
    cp = _connection(
        "control-plane", "control-plane", required=True, configured=cp_configured, applied=True,
        config_revision=cp_revision, effective_revision=cp_revision, requires_restart=False,
        source="infra", observation=cp_observation, now=now, target=cp_target,
    )
    try:
        execution, execution_support = _runtime_connection(now)
    except Exception as exc:  # noqa: BLE001 - isolate a failing domain read
        logger.warning("readiness execution read failed: %s", type(exc).__name__)
        execution, execution_support = _unavailable_connection("execution", now), []
    try:
        ingress, ingress_support = _ingress_connection(now)
    except Exception as exc:  # noqa: BLE001 - isolate a failing domain read
        logger.warning("readiness ingress read failed: %s", type(exc).__name__)
        ingress, ingress_support = _unavailable_connection("ingress", now), []
    try:
        policy, policy_checks = _policy_connection(now)
    except Exception as exc:  # noqa: BLE001 - isolate a failing domain read
        logger.warning("readiness policy read failed: %s", type(exc).__name__)
        policy = _unavailable_connection("policy", now)
        policy_checks = {"l5_admin": False, "active_version": False, "valid": False}
    try:
        optionals = _optional_facts(now)
    except Exception as exc:  # noqa: BLE001 - isolate an optional domain read
        logger.warning("readiness optional read failed: %s", type(exc).__name__)
        optionals = {
            "mcp": {"selected": False, "complete": False},
            "knowledge": {"selected": False, "complete": False},
            "notifications": {"selected": False, "complete": False},
        }
    return {
        "checked_at": now.isoformat(),
        "environment": {
            "backend": backend,
            "database": database,
            "control_plane": {
                "ok": cp["state"] == "healthy", "code": cp["_code"],
                "checked_at": cp["checked_at"],
            },
        },
        "required_connections": [cp, execution, ingress, policy],
        "supporting_connections": execution_support + ingress_support,
        "policy_checks": policy_checks,
        "optionals": optionals,
    }


def _public_connection(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if not k.startswith("_") and v is not None}


def aggregate_readiness(snapshot: dict[str, Any]) -> dict[str, Any]:
    required = snapshot["required_connections"]
    failed = [c for c in required if c["state"] == "failed"]
    warnings = [c for c in required if c["state"] == "warning"]
    overall = "failed" if failed else ("warning" if warnings else "healthy")
    severity_order = {"critical": 0, "warning": 1}
    problems = []
    for conn in required:
        if conn["state"] == "healthy":
            continue
        severity = "critical" if conn["state"] == "failed" else "warning"
        problems.append({
            "kind": conn["kind"],
            "severity": severity,
            "code": conn["_code"],
            "message_key": f"admin.readiness.problems.{str(conn['_code']).lower()}",
            "next_action": _NEXT_ACTIONS.get(conn["id"], _NEXT_ACTIONS["environment"]),
        })
    problems.sort(key=lambda p: severity_order[p["severity"]])
    connections = required + snapshot.get("supporting_connections", [])
    return {
        "overall": overall,
        "required_total": len(required),
        "required_healthy": sum(c["state"] == "healthy" for c in required),
        "checked_at": snapshot["checked_at"],
        "problems": problems[:3],
        "connections": [_public_connection(c) for c in connections],
    }


def _blocking(code: str, step: str) -> dict[str, Any]:
    return {
        "code": code,
        "message_key": f"admin.setup.checks.{str(code).lower()}",
        "next_action": _NEXT_ACTIONS.get(step, _NEXT_ACTIONS["environment"]),
    }


def build_setup_progress(snapshot: dict[str, Any], readiness: dict[str, Any] | None = None) -> dict[str, Any]:
    readiness = readiness or aggregate_readiness(snapshot)
    required_by_id = {c["id"]: c for c in snapshot["required_connections"]}
    environment = snapshot["environment"]
    env_checks = [
        (environment["backend"]["ok"], environment["backend"]["code"]),
        (environment["database"]["ok"], environment["database"]["code"]),
        (environment["control_plane"]["ok"], environment["control_plane"]["code"]),
    ]
    execution = required_by_id["execution"]
    runtime_checks = [
        (execution["_configured"], "REQUIRED_MISSING"),
        (execution["applied"], "NOT_APPLIED"),
        (execution["state"] == "healthy", execution["_code"]),
    ]
    ingress = required_by_id["ingress"]
    ingress_checks = [
        (ingress["_configured"], "REQUIRED_MISSING"),
        (ingress["applied"], "NOT_APPLIED"),
        (ingress["state"] == "healthy", ingress["_code"]),
    ]
    policy_facts = snapshot["policy_checks"]
    policy_checks = [
        (policy_facts["l5_admin"], "L5_ADMIN_MISSING"),
        (policy_facts["active_version"], "ACTIVE_POLICY_MISSING"),
        (policy_facts["valid"], "POLICY_INVALID"),
    ]

    step_specs: list[tuple[str, str, list[tuple[bool, str]], bool]] = [
        ("environment", "required", env_checks, True),
        ("runtime", "required", runtime_checks, execution["_configured"]),
        # Ingress is intentionally required: console-only mode is unsupported.
        ("ingress", "required", ingress_checks, ingress["_configured"]),
        ("policy", "required", policy_checks, policy_facts["l5_admin"] or policy_facts["active_version"]),
    ]
    base_complete = all(all(ok for ok, _ in checks) for _, _, checks, _ in step_specs)
    verify_established = all(established for _, _, _, established in step_specs)
    blocking_zero = not any(c["state"] == "failed" for c in snapshot["required_connections"])
    verify_checks = [(base_complete, "REQUIRED_STEPS_INCOMPLETE"), (blocking_zero, "BLOCKING_FAILURE")]
    step_specs.extend([
        ("mcp", "optional", [(snapshot["optionals"]["mcp"]["complete"], "MCP_CHECK_REQUIRED")], snapshot["optionals"]["mcp"]["selected"]),
        ("knowledge", "optional", [(snapshot["optionals"]["knowledge"]["complete"], "KNOWLEDGE_CHECK_REQUIRED")], snapshot["optionals"]["knowledge"]["selected"]),
        ("notifications", "optional", [(snapshot["optionals"]["notifications"]["complete"], "NOTIFICATION_CHECK_REQUIRED")], snapshot["optionals"]["notifications"]["selected"]),
        ("verify", "required", verify_checks, verify_established),
    ])

    steps = []
    required_passed = 0
    required_total = 0
    for step_id, kind, checks, established in step_specs:
        complete = all(ok for ok, _ in checks)
        optional_skipped = kind == "optional" and not established
        if complete:
            status = "complete"
        elif optional_skipped:
            status = "skipped"
        elif established or any(ok for ok, _ in checks):
            status = "needs_attention"
        else:
            status = "incomplete"
        if kind == "required":
            required_total += len(checks)
            required_passed += sum(bool(ok) for ok, _ in checks)
        steps.append({
            "id": step_id,
            "kind": kind,
            "status": status,
            "checked_at": snapshot["checked_at"],
            "blocking_checks": [_blocking(code, step_id) for ok, code in checks if not ok],
            "optional_skipped": optional_skipped,
        })
    required_steps = [s for s in steps if s["kind"] == "required"]
    required_complete = all(s["status"] == "complete" for s in required_steps)
    current = next((s["id"] for s in required_steps if s["status"] != "complete"), "verify")
    return {
        "schema_version": 1,
        "current_step": current,
        "percent": round(required_passed * 100 / required_total) if required_total else 0,
        "required_complete": required_complete,
        "steps": steps,
    }


@router.get("/v1/admin/readiness")
def admin_readiness(admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:  # noqa: B008
    return aggregate_readiness(_collect_snapshot())


@router.get("/v1/setup/progress")
def setup_progress(admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:  # noqa: B008
    snapshot = _collect_snapshot()
    return build_setup_progress(snapshot, aggregate_readiness(snapshot))


def _clean_candidate_cache(now: float) -> None:
    expired = [key for key, (expires, _) in _candidate_refs.items() if expires <= now]
    for key in expired:
        _candidate_refs.pop(key, None)


def _register_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    candidate_id = "cand_" + secrets.token_urlsafe(12)
    now = time.monotonic()
    with _cache_lock:
        _clean_candidate_cache(now)
        _candidate_refs[candidate_id] = (now + _CANDIDATE_TTL_SECONDS, dict(candidate))
    return {
        "candidate_id": candidate_id,
        "kind": candidate["kind"],
        "display_target": _target_display(candidate.get("target")) or candidate.get("display_target") or candidate["kind"],
        "source": candidate["source"],
        "confidence": candidate["confidence"],
        "credential_state": candidate["credential_state"],
        "applied": candidate["applied"],
        "requires_restart": candidate["requires_restart"],
    }


def _manifest_candidates(kind: str) -> list[dict[str, Any]]:
    raw = os.environ.get("OAOS_ADMIN_DISCOVERY_MANIFEST_JSON", "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        entries = parsed.get(kind, []) if isinstance(parsed, dict) else []
        if isinstance(entries, dict):
            entries = [entries]
        out = []
        for entry in entries[:10]:
            if not isinstance(entry, dict) or not entry.get("target"):
                continue
            credential_state = str(entry.get("credential_state") or "missing")
            if credential_state not in {"available", "authorization_required", "missing"}:
                credential_state = "missing"
            out.append({
                "kind": kind, "target": str(entry["target"]), "source": "manifest",
                "confidence": "medium", "credential_state": credential_state,
                "applied": bool(entry.get("applied", False)),
                "requires_restart": bool(entry.get("requires_restart", True)),
            })
        return out
    except (ValueError, TypeError, AttributeError):
        return []


def _registry_candidates(kind: str) -> list[dict[str, Any]]:
    registry_name = "control-plane" if kind == "control-plane" else kind
    try:
        infra = _domain("infra")
        rows = infra._db_list_services() or []
        out = []
        for row in rows:
            if getattr(row, "name", None) != registry_name:
                continue
            host, port = getattr(row, "host", None), getattr(row, "port", None)
            if not host or not port:
                continue
            out.append({
                "kind": kind, "target": f"http://{host}:{port}", "source": "registry",
                "confidence": "medium", "credential_state": "missing",
                "applied": True, "requires_restart": False,
            })
        return out
    except Exception as exc:  # noqa: BLE001 - optional registry source
        logger.debug("discovery registry read failed: %s", type(exc).__name__)
        return []


def _env_candidate(kind: str) -> list[dict[str, Any]]:
    if kind == "oauth":
        google = bool(os.environ.get("GOOGLE_CLIENT_ID", "").strip() and os.environ.get("GOOGLE_CLIENT_SECRET", "").strip())
        microsoft = bool(
            (os.environ.get("MICROSOFT_CLIENT_ID") or os.environ.get("MS_CLIENT_ID") or "").strip()
            and (os.environ.get("MICROSOFT_CLIENT_SECRET") or os.environ.get("MS_CLIENT_SECRET") or "").strip()
        )
        if not (google or microsoft):
            return []
        return [{
            "kind": "oauth", "target": "oauth-provider", "source": "env", "confidence": "high",
            "credential_state": "available", "applied": True, "requires_restart": False,
        }]
    specs: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
        "control-plane": (("OAOS_CONTROL_PLANE_URL", "OAOS_CP_URL"), ()),
        "acp": (("OAOS_CP_HERMES_BASE_URL", "HERMES_BASE_URL"), ("OAOS_CP_HERMES_API_KEY", "HERMES_API_KEY")),
        "mattermost": (("MATTERMOST_URL",), ("MATTERMOST_TOKEN", "MATTERMOST_BOT_TOKEN")),
        "slack": (("SLACK_WEBHOOK_URL", "SLACK_INCOMING_WEBHOOK_URL", "OAOS_SLACK_WEBHOOK_URL"), ("SLACK_WEBHOOK_URL", "SLACK_INCOMING_WEBHOOK_URL", "OAOS_SLACK_WEBHOOK_URL")),
        "outline": (("OUTLINE_API_URL", "OUTLINE_URL"), ("OUTLINE_API_KEY",)),
        "notion": (("NOTION_API_URL", "OAOS_NOTION_API_URL"), ("NOTION_API_KEY", "NOTION_TOKEN", "OAOS_NOTION_TOKEN")),
        "smtp": (("SMTP_HOST", "OAOS_SMTP_HOST"), ("SMTP_PASSWORD", "OAOS_SMTP_PASSWORD")),
        "mcp": (("OAOS_MCP_URL",), ("OAOS_MCP_TOKEN",)),
    }
    target_keys, credential_keys = specs[kind]
    target = next((os.environ.get(key, "").strip() for key in target_keys if os.environ.get(key, "").strip()), "")
    if not target:
        return []
    if kind == "smtp" and "://" not in target:
        target = f"{target}:{os.environ.get('SMTP_PORT') or os.environ.get('OAOS_SMTP_PORT') or '587'}"
    available = any(os.environ.get(key, "").strip() for key in credential_keys) if credential_keys else True
    return [{
        "kind": kind, "target": target, "source": "env", "confidence": "high",
        "credential_state": "available" if available else "missing",
        "applied": True, "requires_restart": False,
    }]


def _saved_candidate(kind: str) -> list[dict[str, Any]]:
    if kind == "mcp":
        try:
            mcp = _domain("mcp_config")
            servers, _ = mcp._load_servers()
            return [
                {
                    "kind": "mcp", "target": str(cfg.get("url") or cfg.get("command")),
                    "source": "saved", "confidence": "low",
                    "credential_state": "available" if cfg.get("headers") or cfg.get("transport") == "stdio" else "missing",
                    "applied": True, "requires_restart": False,
                }
                for cfg in servers.values()
                if isinstance(cfg, dict) and (cfg.get("url") or cfg.get("command"))
            ]
        except Exception:  # noqa: BLE001 - unavailable MCP registry means no candidate
            return []
    modules = {
        "acp": ("acp_config", "hermes_base_url", lambda c: bool(c.get("api_key_set")) or True),
        "mattermost": ("mattermost_config", "mattermost_url", lambda c: bool(c.get("bot_token_set"))),
        "outline": ("outline_config", "outline_url", lambda c: bool(c.get("api_key_set"))),
        "notion": ("notion_config", "notion_api_url", lambda c: bool(c.get("api_key_set"))),
        "smtp": ("smtp_config", "smtp_host", lambda c: not c.get("smtp_user") or bool(c.get("smtp_password_set"))),
    }
    spec = modules.get(kind)
    if not spec:
        return []
    try:
        mod = _domain(spec[0])
        cfg, source = mod._load_config()
        if source not in {"db", "in-memory"} or not cfg.get(spec[1]):
            return []
        target = str(cfg[spec[1]])
        if kind == "smtp":
            target = f"{target}:{cfg.get('smtp_port', 587)}"
        return [{
            "kind": kind, "target": target, "source": "saved", "confidence": "low",
            "credential_state": "available" if spec[2](cfg) else "missing",
            "applied": False, "requires_restart": True,
        }]
    except Exception:  # noqa: BLE001 - unavailable saved source means no candidate
        return []


def discover_candidates(kind: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    candidates.extend(_env_candidate(kind))
    candidates.extend(_manifest_candidates(kind))
    candidates.extend(_registry_candidates(kind))
    for target in _DEFAULT_TARGETS.get(kind, ()):
        candidates.append({
            "kind": kind, "target": target, "source": "default", "confidence": "low",
            "credential_state": "available" if kind in {"control-plane", "acp"} else "missing",
            "applied": False, "requires_restart": kind not in {"control-plane"},
        })
    # OAuth-first for SaaS when no injected authorization is available.
    if kind in {"slack", "notion", "oauth"} and not any(c["credential_state"] == "available" for c in candidates):
        candidates.append({
            "kind": kind, "target": f"{kind}.com", "source": "default", "confidence": "high",
            "credential_state": "authorization_required", "applied": False,
            "requires_restart": False,
        })
    candidates.extend(_saved_candidate(kind))
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate["source"], str(candidate.get("target")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    public = [_register_candidate(c) for c in deduped]
    return {
        "kind": kind,
        "candidates": public,
        "reasons": [] if public else ["admin.connections.discovery.noCandidates"],
    }


@router.get("/v1/admin/connections/discovery")
def connection_discovery(
    kind: str = Query(min_length=1, max_length=64),
    admin: AdminUser = Depends(get_current_admin),  # noqa: B008
) -> dict[str, Any]:
    normalized = kind.strip().lower()
    if normalized not in _SUPPORTED_KINDS:
        return {"kind": normalized, "candidates": [], "reasons": ["admin.connections.discovery.unsupportedKind"]}
    return discover_candidates(normalized)


def _get_candidate(candidate_id: str | None, connection_id: str) -> dict[str, Any] | None:
    if not candidate_id:
        return None
    now = time.monotonic()
    with _cache_lock:
        _clean_candidate_cache(now)
        stored = _candidate_refs.get(candidate_id)
    if not stored:
        raise _AdapterFailure("MISCONFIGURED")
    candidate = dict(stored[1])
    base_id = connection_id.split(":", 1)[0]
    if candidate.get("kind") != base_id:
        raise _AdapterFailure("MISCONFIGURED")
    return candidate


def _connection_config(connection_id: str) -> dict[str, Any]:
    base = connection_id.split(":", 1)[0]
    module_names = {
        "acp": "acp_config", "mattermost": "mattermost_config", "slack": "slack_config",
        "outline": "outline_config", "notion": "notion_config", "smtp": "smtp_config",
    }
    if base in module_names:
        mod = _domain(module_names[base])
        cfg, source = mod._load_config()
        meta = apply_state_fields(base, cfg, source, effective_config=mod._env_config())
        target_keys = {"acp": "hermes_base_url", "mattermost": "mattermost_url", "outline": "outline_url", "notion": "notion_api_url", "smtp": "smtp_host"}
        target = cfg.get(target_keys.get(base, ""))
        if base == "slack":
            target = "slack.com"
        if base == "smtp" and target:
            target = f"{target}:{cfg.get('smtp_port', 587)}"
        return {"config": cfg, "source": source, "target": target, **meta}
    if base == "oauth":
        mod = _domain("oauth_config")
        prefs, source = mod._load_prefs()
        cfg = {**mod._env_config(), **prefs}
        revision = public_revision("oauth", cfg)
        return {
            "config": cfg, "source": source, "target": "oauth-provider",
            "persisted": True, "applied": True, "config_revision": revision,
            "effective_revision": revision, "requires_restart": False,
        }
    if base == "control-plane":
        revision = public_revision("control-plane", {"target": "127.0.0.1:8100"})
        return {"config": {}, "source": "default", "target": "http://127.0.0.1:8100", "persisted": True, "applied": True, "config_revision": revision, "effective_revision": revision, "requires_restart": False}
    if base == "mcp" and ":" in connection_id:
        mcp = _domain("mcp_config")
        servers, source = mcp._load_servers()
        cfg = servers.get(connection_id.split(":", 1)[1])
        if not isinstance(cfg, dict):
            raise _AdapterFailure("MISCONFIGURED")
        revision = public_revision(connection_id, {k: v for k, v in cfg.items() if k != "headers"})
        return {"config": cfg, "source": source, "target": cfg.get("url") or cfg.get("command"), "persisted": True, "applied": True, "config_revision": revision, "effective_revision": revision, "requires_restart": False}
    if base == "mcp":
        revision = public_revision("mcp", {"discovery": True})
        return {"config": {"transport": "streamable-http"}, "source": "discovery", "target": None, "persisted": False, "applied": False, "config_revision": revision, "effective_revision": revision, "requires_restart": False}
    raise _AdapterFailure("MISCONFIGURED")


def _http_result(response: Any, started: float, target: str) -> dict[str, Any]:
    return {
        "status_code": int(response.status_code),
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
        "target_display": _target_display(target),
    }


def _run_adapter(
    connection_id: str,
    config_info: dict[str, Any],
    candidate: dict[str, Any] | None,
    timeout_seconds: float,
    mode: str,
) -> dict[str, Any]:
    base = connection_id.split(":", 1)[0]
    cfg = config_info["config"]
    target = str((candidate or {}).get("target") or config_info.get("target") or "")
    started = time.monotonic()
    if base == "smtp":
        host = target.rsplit(":", 1)[0]
        port = int(target.rsplit(":", 1)[1]) if ":" in target else 587
        client = smtplib.SMTP_SSL(host, port, timeout=timeout_seconds) if port == 465 else smtplib.SMTP(host, port, timeout=timeout_seconds)
        try:
            client.ehlo()
        finally:
            try:
                client.quit()
            except (OSError, smtplib.SMTPException):
                client.close()
        return {"status_code": 200, "latency_ms": round((time.monotonic() - started) * 1000, 1), "target_display": _target_display(target)}

    import httpx
    if base == "oauth":
        google_ready = bool(cfg.get("google_enabled") and cfg.get("google_client_id_set") and cfg.get("google_client_secret_set"))
        microsoft_ready = bool(cfg.get("microsoft_enabled") and cfg.get("microsoft_client_id_set") and cfg.get("microsoft_client_secret_set"))
        if not (google_ready or microsoft_ready):
            raise _AdapterFailure("AUTH_REQUIRED")
        discovery_target = "https://accounts.google.com/.well-known/openid-configuration" if google_ready else "https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration"
        response = httpx.get(discovery_target, timeout=timeout_seconds)
        return _http_result(response, started, discovery_target)
    if base == "slack":
        webhook = next((os.environ.get(k, "").strip() for k in ("SLACK_WEBHOOK_URL", "SLACK_INCOMING_WEBHOOK_URL", "OAOS_SLACK_WEBHOOK_URL") if os.environ.get(k, "").strip()), "")
        if not webhook:
            raise _AdapterFailure("AUTH_REQUIRED")
        if mode == "safe":
            return {"status_code": 200, "latency_ms": round((time.monotonic() - started) * 1000, 1), "target_display": _target_display(webhook)}
        response = httpx.post(webhook, json={"text": "OAOS admin connection test"}, timeout=timeout_seconds)
        return _http_result(response, started, webhook)
    if base == "mattermost":
        token = os.environ.get("MATTERMOST_TOKEN") or os.environ.get("MATTERMOST_BOT_TOKEN")
        if not token:
            raise _AdapterFailure("AUTH_REQUIRED")
        response = httpx.get(target.rstrip("/") + "/api/v4/users/me", headers={"Authorization": f"Bearer {token}"}, timeout=timeout_seconds)
        return _http_result(response, started, target)
    if base == "outline":
        key = os.environ.get("OUTLINE_API_KEY")
        if not key:
            raise _AdapterFailure("AUTH_REQUIRED")
        response = httpx.post(target.rstrip("/") + "/api/collections.list", json={"token": key}, timeout=timeout_seconds)
        return _http_result(response, started, target)
    if base == "notion":
        key = os.environ.get("NOTION_API_KEY") or os.environ.get("NOTION_TOKEN") or os.environ.get("OAOS_NOTION_TOKEN")
        if not key:
            raise _AdapterFailure("AUTH_REQUIRED")
        response = httpx.get(target.rstrip("/") + "/v1/users", headers={"Authorization": f"Bearer {key}", "Notion-Version": "2022-06-28"}, timeout=timeout_seconds)
        return _http_result(response, started, target)
    if base == "mcp":
        if cfg.get("transport") == "stdio":
            if not cfg.get("command"):
                raise _AdapterFailure("MISCONFIGURED")
            return {"status_code": 200, "latency_ms": 0.0, "target_display": str(cfg["command"])[:80]}
        response = httpx.post(target, json={"jsonrpc": "2.0", "id": "health", "method": "tools/list", "params": {}}, headers=cfg.get("headers") or {}, timeout=timeout_seconds)
        return _http_result(response, started, target)
    path = "/health"
    response = httpx.get(target.rstrip("/") + path, timeout=timeout_seconds)
    return _http_result(response, started, target)


_CODE_MESSAGES = {
    "OK": ("Connection is healthy", "admin.connections.test.ok"),
    "AUTH_REQUIRED": ("Authorization is required", "admin.connections.test.authRequired"),
    "PERMISSION_DENIED": ("Permission was denied", "admin.connections.test.permissionDenied"),
    "UNREACHABLE": ("Connection target is unreachable", "admin.connections.test.unreachable"),
    "TIMEOUT": ("Connection test timed out", "admin.connections.test.timeout"),
    "NOT_APPLIED": ("Saved configuration is not applied", "admin.connections.test.notApplied"),
    "MISCONFIGURED": ("Connection is not configured correctly", "admin.connections.test.misconfigured"),
    "UNKNOWN": ("Connection test failed", "admin.connections.test.unknown"),
}


def _envelope(
    connection_id: str,
    code: str,
    correlation_id: str,
    *,
    applied: bool,
    requires_restart: bool,
    checked_at: str,
    result: dict[str, Any] | None = None,
    authorization_pending: bool = False,
) -> dict[str, Any]:
    result = result or {}
    summary, message_key = _CODE_MESSAGES.get(code, _CODE_MESSAGES["UNKNOWN"])
    status = "healthy" if code == "OK" else (
        "warning" if code == "NOT_APPLIED" or authorization_pending else "failed"
    )
    body: dict[str, Any] = {
        "ok": code == "OK",
        "status": status,
        "code": code if code in _CODE_MESSAGES else "UNKNOWN",
        "summary": summary,
        "message_key": message_key,
        "checked_at": checked_at,
        "applied": applied,
        "requires_restart": requires_restart,
        "correlation_id": correlation_id,
    }
    if result.get("latency_ms") is not None:
        body["latency_ms"] = result["latency_ms"]
    display = _target_display(result.get("target_display"))
    if display:
        body["target_display"] = display
    if code != "OK":
        body["next_action"] = _NEXT_ACTIONS.get(connection_id.split(":", 1)[0], _NEXT_ACTIONS["environment"])
    return body


def _normalize_exception(exc: Exception) -> str:
    if isinstance(exc, _AdapterFailure):
        return exc.code
    try:
        import httpx
        if isinstance(exc, httpx.TimeoutException):
            return "TIMEOUT"
        if isinstance(exc, httpx.ConnectError):
            return "UNREACHABLE"
    except ImportError:
        pass
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "TIMEOUT"
    if isinstance(exc, (ConnectionError, OSError, smtplib.SMTPConnectError)):
        return "UNREACHABLE"
    return "UNKNOWN"


def _request_timeout(header_value: str | None, quick: bool) -> float:
    default_ms = 5000 if quick else 8000
    if not header_value:
        return default_ms / 1000
    try:
        requested = int(header_value)
        return max(0.1, min(requested, default_ms) / 1000)
    except (TypeError, ValueError):
        return default_ms / 1000


@router.post("/v1/admin/connections/{connection_id}/test")
def test_connection(
    connection_id: str,
    body: TestConnectionRequest,
    request_timeout_ms: str | None = Header(default=None, alias="X-Request-Timeout-Ms"),
    admin: AdminUser = Depends(require_l5),  # noqa: B008
) -> dict[str, Any]:
    correlation_id = "conn_" + uuid.uuid4().hex
    checked_at = utc_now_iso()
    code = "UNKNOWN"
    result: dict[str, Any] = {}
    applied = False
    requires_restart = False
    authorization_pending = False
    valid_connection = False
    try:
        candidate = _get_candidate(body.candidate_id, connection_id)
        config_info = _connection_config(connection_id)
        valid_connection = True
        applied = bool(candidate.get("applied")) if candidate else bool(config_info["applied"])
        requires_restart = bool(candidate.get("requires_restart")) if candidate else bool(config_info["requires_restart"])
        result = {
            "target_display": _target_display(
                str((candidate or {}).get("target") or config_info.get("target") or "")
            )
        }
        if (
            body.config_revision and body.config_revision != config_info["effective_revision"]
        ) or (not applied and not candidate):
            code = "NOT_APPLIED"
        elif candidate and candidate.get("credential_state") in {"authorization_required", "missing"}:
            code = "AUTH_REQUIRED"
            authorization_pending = True
        else:
            timeout = _request_timeout(request_timeout_ms, connection_id.startswith("control-plane"))
            result = _run_adapter(connection_id, config_info, candidate, timeout, body.mode)
            status_code = int(result.get("status_code", 0))
            if 200 <= status_code < 400:
                code = "OK"
            elif status_code == 401:
                code = "AUTH_REQUIRED"
            elif status_code == 403:
                code = "PERMISSION_DENIED"
            else:
                code = "UNREACHABLE"
    except Exception as exc:  # noqa: BLE001 - normalize every adapter failure
        code = _normalize_exception(exc)
    envelope = _envelope(
        connection_id, code, correlation_id, applied=applied,
        requires_restart=requires_restart, checked_at=checked_at, result=result,
        authorization_pending=authorization_pending,
    )
    if valid_connection:
        _record_observation(connection_id, envelope)
    logger.info(
        "admin connection test correlation_id=%s connection_id=%s code=%s",
        correlation_id, connection_id[:96], envelope["code"],
    )
    return envelope
