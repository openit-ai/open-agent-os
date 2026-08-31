"""Mattermost webhook → Control Plane → ACP → Hermes (Section 37).

Flow:
  Mattermost event (user message / mention)
    → verify signature (HMAC)
    → map Mattermost user → employee: principal
    → create or resume session (Workstream A)
    → forward prompt via ACPAdapter
    → stream response back to Mattermost (Bot post)

For Workstream A MVP: webhook accepts generic JSON (no real Mattermost server required).
Real verification uses MATTERMOST_WEBHOOK_SECRET.

Extended for Phase 1 MVP (Section 3.1):
  If text contains "정리해줘" keyword, route to morning-briefing orchestrator
  and return briefing JSON directly (demo parity with POST /v1/demo/morning-briefing).

Hardened v1.5.1:
  - POST /v1/mattermost/events  (existing, now with background streaming)
  - POST /v1/mattermost/slash   (slash commands)
  - POST /v1/mattermost/actions (interactive 4-button approval)
  §§16A, 23
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from ..acp_adapter import ACPAdapter
from ..config import settings
from ..identity import map_user_to_agent
from ..router import route_session
from ..session import new_request_id, session_store

router = APIRouter()


def _archive_conversation_turn(tenant_id: str, agent_id: str, user_id: str, session_id: str, request_id: str, text: str) -> None:
    """Append a user turn to the owner-scoped Personal Wiki journal.

    This is deliberately local and deterministic: no LLM/OCR/provider is
    involved. Failure is logged but never changes the conversational response.
    """
    try:
        import sys
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[3]
        package_root = repo_root / "packages" / "personal-wiki"
        if str(package_root) not in sys.path:
            sys.path.insert(0, str(package_root))
        from personal_wiki.vault import append_journal, vault_path_for_tenant_agent
        owner_root = vault_path_for_tenant_agent(tenant_id, agent_id)
        append_journal(
            trace_id=request_id,
            tool_name="mattermost_conversation",
            result={"user_id": user_id, "session_id": session_id, "text": text},
            # tenant/agent are already derived from the verified server-side
            # Mattermost identity and owner_root is the isolated target.
            extra={"tenant_id": tenant_id, "agent_id": agent_id, "source": "mattermost"},
            vault_root=owner_root,
        )
    except Exception as exc:
        log.warning("personal wiki conversation archive failed session=%s request=%s: %s", session_id, request_id, exc)

log = logging.getLogger(__name__)

# ── Small-business Mattermost -> ACP -> Policy Engine gate ─────────────
# Deterministic gate: identity/ownership validated + PolicyEngine evaluated
# BEFORE ACP/LLM forwarding. Every ingress (including low-risk INTERACT)
# produces a POLICY_DECISION audit event. Explicit DENY overrides.
# Reuses existing PolicyEngine / AuthorizationHook / AuditLedger via mattermost_policy_gate.
# Ordinary chat is low-risk INTERACT but still audited.
_INGRESS_ACTION = "INTERACT"  # low-risk conversational prompt

def _get_policy_engine(tenant_id: str):  # compat shim — delegates to mattermost_policy_gate
    try:
        from ..mattermost_policy_gate import _get_small_business_engine  # type: ignore
        return _get_small_business_engine(tenant_id)
    except Exception:
        return None

def _get_audit_ledger():  # compat shim
    try:
        from ..mattermost_policy_gate import _get_audit_ledger as _gal  # type: ignore
        return _gal()
    except Exception:
        return None

def _emit_policy_audit(*args, **kwargs):  # compat shim — fail-closed via gate
    from ..mattermost_policy_gate import _emit_policy_audit as _epa  # type: ignore
    return _epa(*args, **kwargs)

async def _evaluate_ingress_policy(
    *,
    tenant_id: str,
    mapping: Any,
    session_id: str,
    trace_id: str,
    request_id: str,
    channel_id: str | None = None,
) -> tuple[str, str, str | None]:
    """Evaluate low-risk ingress via deterministic gate (AuthorizationHook + small-business bundle).

    Always produces POLICY_DECISION audit (fail-closed). Raises HTTPException 403 on DENY/APPROVAL_REQUIRED.
    """
    try:
        from ..mattermost_policy_gate import get_mattermost_gate  # type: ignore
        gate = get_mattermost_gate(tenant_id)
        result = await gate.authorize_ingress(mapping, session_id, trace_id, request_id, channel_id)
        # result is AuthzResult-like
        decision = getattr(result, "decision", "ALLOW")
        reason = getattr(result, "reason", "")
        pv = getattr(result, "matched_rule_id", None) or getattr(result, "policy_version", None)
        return decision, reason, pv
    except HTTPException:
        raise
    except Exception as e:
        # Fallback direct (should not happen) — fail-closed
        raise HTTPException(status_code=403, detail=f"policy denied: gate error: {e}")

# Lazy import for orchestrator (avoid circular at import time)
def _load_orchestrator():
    ROOT = Path(__file__).resolve().parents[3]
    for p in [
        ROOT / "examples" / "morning-briefing",
        ROOT / "execution-gateway",
        ROOT / "security" / "policy-engine",
        ROOT / "packages" / "policy-model",
        ROOT / "packages" / "audit-model",
        ROOT / "packages" / "common-types",
    ]:
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    try:
        from orchestrator import run_morning_briefing  # type: ignore

        return run_morning_briefing
    except Exception:
        try:
            from morning_briefing.orchestrator import run_morning_briefing  # type: ignore

            return run_morning_briefing
        except Exception:
            return None


BRIEFING_KEYWORDS = ["정리해줘", "브리핑", "업무 정리", "오늘 업무"]

# 4-button approval decisions §23
VALID_DECISIONS = {"DENIED", "APPROVED_ONCE", "APPROVED_USER_ALWAYS", "APPROVED_GROUP_ALWAYS"}


def _is_briefing_request(text: str) -> bool:
    return any(kw in text for kw in BRIEFING_KEYWORDS)


def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        if os.getenv(k, "").strip().lower() in ("production", "prod"):
            return True
    return False

def verify_mattermost_signature(body: bytes, signature: str | None, secret: str | None) -> bool:
    # Production: fail-closed — empty secret or missing signature must deny
    if not secret:
        return False if _is_production() else True  # dev: no secret configured → accept; prod → deny
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

def _resolve_tenant_id(payload_tenant: str | None) -> str:
    # Never trust payload tenant_id — always use server-configured tenant
    # (payload HMAC proves it came from Mattermost, but tenant scoping is server authority)
    _ = payload_tenant  # explicitly ignored
    return settings.tenant_id

def _allow_test_identity() -> bool:
    # Only allow arbitrary employee: payload identities when explicitly marked as internal test AND non-production
    if _is_production():
        return False
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    for k in ("OAOS_ALLOW_TEST_IDENTITY", "OAOS_ALLOW_TEST_FIXTURE", "OAOS_ALLOW_TEST_FALLBACK"):
        if os.getenv(k, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False

def _resolve_user_id(raw_user_id: str, raw_user_name: str | None = None) -> str:
    # Map Mattermost user id/username → employee: principal deterministically — never trust payload as-is if not namespaced
    # Arbitrary employee: identities are blocked unless _allow_test_identity() (internal test + non-production)
    uid = (raw_user_id or "").strip()
    uname = (raw_user_name or "").strip() or None
    if uid.startswith("employee:"):
        if _allow_test_identity():
            return uid
        # Non-test: do not trust payload employee: — derive via adapter from raw suffix
        suffix = uid.split(":", 1)[1].strip()
        try:
            adapter = _get_mattermost_adapter()
            if adapter is not None:
                return adapter.map_mattermost_user(suffix or uid, uname or suffix)
        except Exception:
            pass
        import re as _re2
        raw2 = uname or suffix or uid
        suf2 = _re2.sub(r"[^a-z0-9_.-]", "", raw2.lower()) or "unknown"
        return f"employee:{suf2}"
    # Try adapter mapping for deterministic derivation
    try:
        adapter = _get_mattermost_adapter()
        if adapter is not None:
            return adapter.map_mattermost_user(uid, uname)
    except Exception:
        pass
    # Fallback deterministic sanitize
    import re as _re
    raw = uname or uid
    suffix = _re.sub(r"[^a-z0-9_.-]", "", raw.lower()) or "unknown"
    return f"employee:{suffix}"


def _get_mattermost_adapter():
    """Lazy MattermostAdapter (avoid hard dep if adapters not installed)."""
    try:
        # Use absolute import via path insertion
        ROOT = Path(__file__).resolve().parents[3]
        p = ROOT / "adapters"
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
        from mattermost.adapter import MattermostAdapter  # type: ignore

        base_url = getattr(settings, "mattermost_url", "") or os.getenv("MATTERMOST_URL", "")
        bot_token = getattr(settings, "mattermost_bot_token", "") or os.getenv("MATTERMOST_BOT_TOKEN", "")
        webhook_secret = getattr(settings, "mattermost_webhook_secret", "") or os.getenv("MATTERMOST_WEBHOOK_SECRET", "")
        if not bot_token:
            try:
                for line in (Path.home() / ".hermes" / ".env").read_text(encoding="utf-8").splitlines():
                    key, sep, value = line.partition("=")
                    if sep and key == "MATTERMOST_BOT_TOKEN":
                        bot_token = value.strip().strip('"').strip("'")
                    elif sep and key == "MATTERMOST_URL" and not base_url:
                        base_url = value.strip().strip('"').strip("'")
            except OSError:
                pass
        return MattermostAdapter(
            base_url=base_url,
            bot_token=bot_token,
            webhook_secret=webhook_secret,
        )
    except Exception:
        # Fallback import relative
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "mm_adapter", str(Path(__file__).resolve().parents[3] / "adapters" / "mattermost" / "adapter.py")
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore
                return mod.MattermostAdapter(
                    base_url=getattr(settings, "mattermost_url", "") or "",
                    bot_token=getattr(settings, "mattermost_bot_token", "") or "",
                    webhook_secret=getattr(settings, "mattermost_webhook_secret", "") or "",
                )
        except Exception:
            return None
def _get_personal_display_name(agent_id: str) -> tuple[str | None, str | None]:
    """Resolve A안 display_name/avatar_url for agent_id from admin_user_mappings (DB if available)."""
    if not agent_id:
        return None, None
    # try DB via SECURITY models / direct psycopg
    try:
        import os
        db_url = os.getenv("OAOS_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
        if db_url:
            # normalize asyncpg / driver
            url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://").replace("postgresql://", "postgresql+psycopg://").replace("+aiosqlite","").replace("sqlite+aiosqlite://","sqlite://")
            # try sqlalchemy
            try:
                from sqlalchemy import create_engine, text as sa_text
                eng = create_engine(url, pool_pre_ping=True, connect_args={"check_same_thread": False} if "sqlite" in url else {})
                with eng.connect() as conn:
                    row = conn.execute(sa_text("SELECT display_name, avatar_url FROM admin_user_mappings WHERE agent_id=:aid LIMIT 1"), {"aid": agent_id}).mappings().first()
                    if row and (row.get("display_name") or row.get("avatar_url")):
                        return row.get("display_name"), row.get("avatar_url")
            except Exception:
                pass
            # try psycopg directly
            try:
                import psycopg  # type: ignore
                with psycopg.connect(url) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT display_name, avatar_url FROM admin_user_mappings WHERE agent_id=%s LIMIT 1", (agent_id,))
                        r = cur.fetchone()
                        if r:
                            return r[0], r[1]
            except Exception:
                pass
    except Exception:
        pass
    return None, None


def _get_approval_store():
    """Obtain ApprovalStore singleton (security/approval)."""
    try:
        ROOT = Path(__file__).resolve().parents[3]
        p = ROOT / "security" / "approval"
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
        from approval_workflow.workflow import ApprovalStore  # type: ignore

        # reuse module-level singleton if already created
        if not hasattr(_get_approval_store, "_store"):
            key = getattr(settings, "mattermost_webhook_secret", "") or "dev-signing-key"
            _get_approval_store._store = ApprovalStore(signing_key=key)  # type: ignore
        return _get_approval_store._store  # type: ignore
    except Exception:
        return None


async def _post_with_retry(adapter: Any, channel_id: str, text: str, root_id: str | None, trace_id: str, session_id: str, display_name: str | None = None, avatar_url: str | None = None) -> str:
    """Post one response with bounded retry; never hide delivery failures. Splits over-long posts. Returns last post_id (compat)."""
    ids = await _post_with_retry_collect(adapter, channel_id, text, root_id, trace_id, session_id, display_name=display_name, avatar_url=avatar_url)
    return ids[-1] if ids else ""

async def _post_with_retry_collect(adapter: Any, channel_id: str, text: str, root_id: str | None, trace_id: str, session_id: str, display_name: str | None = None, avatar_url: str | None = None) -> list[str]:
    """Bounded retry per chunk; returns ALL successful post_ids (empty if all failed). Each chunk tried ≤3 times."""
    if not text.strip():
        return []
    MAX_MM = 4000
    chunks = [text[i:i+MAX_MM] for i in range(0, len(text), MAX_MM)] if len(text) > MAX_MM else [text]
    all_ids: list[str] = []
    for chunk in chunks:
        success = False
        for attempt in range(1, 4):
            try:
                props = {}
                if display_name:
                    props["override_username"] = display_name
                if avatar_url:
                    props["override_icon_url"] = avatar_url
                kwargs = {"root_id": root_id}
                if props:
                    kwargs["props"] = props
                result = await adapter.send_message(channel_id, chunk, **kwargs)
                if isinstance(result, dict) and result.get("_skeleton"):
                    raise RuntimeError("Mattermost adapter returned skeleton response")
                post_id = result.get("id", "") if isinstance(result, dict) else ""
                log.info("mattermost response posted channel=%s root=%s post=%s trace=%s session=%s attempt=%d chunk_len=%d", channel_id, root_id, post_id, trace_id, session_id, attempt, len(chunk))
                if post_id:
                    all_ids.append(post_id)
                else:
                    # empty id treated as failure → retry
                    raise RuntimeError("empty post_id from mattermost adapter")
                success = True
                break
            except Exception as exc:
                log.warning("mattermost response post failed channel=%s root=%s trace=%s session=%s attempt=%d error=%s", channel_id, root_id, trace_id, session_id, attempt, str(exc)[:300], exc_info=attempt == 3)
                if attempt < 3:
                    await asyncio.sleep(0.5 * attempt)
                else:
                    pass
        # if this chunk never succeeded, continue to next chunk but caller can detect partial by len(all_ids) < len(chunks)
        if not success:
            log.error("mattermost chunk ultimately failed channel=%s root=%s trace=%s session=%s chunk_len=%d", channel_id, root_id, trace_id, session_id, len(chunk))
    return all_ids

def _build_response_marker(text: str, channel_id: str | None, root_id: str | None) -> str:
    """Deterministic marker for read-back dedup (hash of channel+root+text prefix)."""
    import hashlib
    raw = f"{channel_id or ''}\x1f{root_id or ''}\x1f{(text or '')[:200]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def _stream_and_post_to_mattermost(
    channel_id: str | None,
    root_id: str | None,
    session_rec: Any,
    idempotency_key: str | None = None,
) -> None:
    """Fetch ACP stream and post responses with bounded retry, idempotency completion, and delivery logging."""
    if not channel_id:
        log.warning("mattermost response skipped: missing channel trace=%s session=%s", getattr(session_rec, "trace_id", ""), getattr(session_rec, "session_id", ""))
        return
    adapter = _get_mattermost_adapter()
    if adapter is None:
        log.error("mattermost adapter unavailable channel=%s root=%s trace=%s session=%s", channel_id, root_id, getattr(session_rec, "trace_id", ""), getattr(session_rec, "session_id", ""))
        return
    trace_id = getattr(session_rec, "trace_id", "")
    session_id = getattr(session_rec, "session_id", "")
    # A안: resolve personal display_name/icon per user (session preferred, DB fallback)
    display_name = getattr(session_rec, "display_name", None)
    avatar_url = getattr(session_rec, "avatar_url", None)
    if not display_name:
        try:
            aid = getattr(session_rec, "agent_id", "") or ""
            dn, av = _get_personal_display_name(aid)
            if dn:
                display_name = dn
            if av and not avatar_url:
                avatar_url = av
        except Exception:
            pass
    buffer = ""
    full_text = ""
    all_response_post_ids: list[str] = []
    any_post_failed = False
    try:
        acp = ACPAdapter(settings.hermes_base_url)
        async for ev in acp.stream_events(session_rec):
            etype = ev.get("type", "")
            if etype == "briefing":
                continue
            if etype == "token":
                chunk_text = ev.get("data", {}).get("text", "") or ev.get("text", "") or ""
                buffer += chunk_text
                full_text += chunk_text
                if len(buffer) > 800 or buffer.endswith("\n"):
                    ids = await _post_with_retry_collect(adapter, channel_id, buffer, root_id, trace_id, session_id, display_name=display_name, avatar_url=avatar_url)
                    if ids:
                        all_response_post_ids.extend(ids)
                    else:
                        # empty ids means this flush's chunks all failed after bounded retry
                        if buffer.strip():
                            any_post_failed = True
                    buffer = ""
            elif etype == "done":
                break
        if buffer.strip():
            ids = await _post_with_retry_collect(adapter, channel_id, buffer, root_id, trace_id, session_id, display_name=display_name, avatar_url=avatar_url)
            if ids:
                all_response_post_ids.extend(ids)
            else:
                any_post_failed = True
        # P0 idempotency: record response_post_ids for read-back + marker; fail-closed on delivery failure
        # delivery complete condition (explicit): ALL chunks succeeded (any_post_failed==False) AND at least one Mattermost post_id exists.
        # Any bound-retry-exhausted chunk → not complete, mark retryable failed so reclaim can retry; partial ids retained in fail record.
        # Marker is deterministic on full response text (or idempotency_key fallback) — never on empty flush buffer.
        if idempotency_key:
            try:
                from control_plane.idempotency import complete as _idem_complete, fail as _idem_fail2
                marker_source = full_text.strip() or idempotency_key
                marker = _build_response_marker(marker_source, channel_id, root_id)
                delivery_complete = (not any_post_failed) and len(all_response_post_ids) > 0
                if not delivery_complete:
                    # delivery failed → do not mark completed; mark retryable failed so reclaim can retry; preserve partial ids
                    _idem_fail2(idempotency_key, error="mattermost delivery failed (bounded retry exhausted)", retryable=True, response_post_ids=all_response_post_ids, response_marker=marker)
                else:
                    _idem_complete(idempotency_key, response_post_id=all_response_post_ids[-1], response_post_ids=all_response_post_ids, response_marker=marker, session_id=session_id, trace_id=trace_id)
            except Exception as _idem_e:
                # if complete/fail itself is 503 in prod, propagate visibly
                from fastapi import HTTPException as _HEc
                if isinstance(_idem_e, _HEc):
                    raise
                pass
    except Exception as exc:
        log.error("mattermost response stream failed channel=%s root=%s trace=%s session=%s error=%s", channel_id, root_id, trace_id, session_id, str(exc)[:500], exc_info=True)
        if idempotency_key:
            try:
                from control_plane.idempotency import fail as _idem_fail, is_retryable_error as _is_retry
                _idem_fail(idempotency_key, error=str(exc)[:500], retryable=_is_retry(exc))
            except Exception:
                pass


async def _handle_core_logic(
    tenant_id: str,
    user_id: str,
    text: str,
    session_id: str | None,
    channel_id: str | None = None,
    post_id: str | None = None,
    root_id: str | None = None,
    file_ids: list[str] | None = None,
    attachment_refs: list[dict] | None = None,
    runtime_context: dict | None = None,
) -> dict[str, Any]:
    """Shared session/briefing/ACP logic (reused by events + slash)."""
    # Identity mapping — 1:1 logical agent
    mapping = map_user_to_agent(user_id, tenant_id)

    # ── P0 Idempotency early gate (before session/policy/LLM side effects) ──
    # If same post_id retries, we must NOT create a new session or call LLM again.
    # So atomic claim happens before any side-effecting step. Policy & session creation
    # only proceed for the first claim; duplicates return read-back immediately.
    _pre_rid = new_request_id()
    _idem_key: str | None = None
    _idem_claim = None
    _idem_early_claimed = False
    if post_id:
        try:
            from control_plane.idempotency import try_claim as _idem_try_claim, build_idempotency_key as _idem_build
            _idem_key = _idem_build(tenant_id, channel_id, post_id)
            if _idem_key is not None:
                # For early gate we have no rec yet; use provided session_id or deterministic pending placeholder
                # trace_id placeholder derived from _pre_rid (real trace will be session's trace, updated on complete)
                _tmp_sid = session_id or f"pending:{post_id[:24]}"
                _tmp_trace = _pre_rid
                _k, _c = _idem_try_claim(tenant_id=tenant_id, channel_id=channel_id, post_id=post_id, session_id=_tmp_sid, trace_id=_tmp_trace, request_id=_pre_rid)
                if _c is not None and _c.is_duplicate:
                    dup_rec = _c.record
                    try:
                        from control_plane.mattermost_policy_gate import _get_audit_ledger as _gal, _emit_policy_audit as _epa  # type: ignore
                        _ledger2 = _gal()
                        if _ledger2 is not None:
                            _epa(_ledger2, tenant_id=tenant_id, user_id=mapping.human_principal, agent_id=mapping.agent_principal, session_id=dup_rec.get("session_id", _tmp_sid), trace_id=dup_rec.get("trace_id", _tmp_trace), request_id=_pre_rid, action="INTERACT", resource=f"session/ingress/{tenant_id}/{_tmp_sid}", decision="ALLOW", policy_version=None, reason=f"idempotency duplicate { _c.status} key={_k} response_post_id={dup_rec.get('response_post_id','')}")
                    except Exception:
                        pass
                    return {
                        "received": True,
                        "duplicate": True,
                        "idempotency_key": _k,
                        "idempotency_status": _c.status,
                        "session_id": dup_rec.get("session_id", _tmp_sid),
                        "agent_id": mapping.agent_principal,
                        "trace_id": dup_rec.get("trace_id", _tmp_trace),
                        "request_id": dup_rec.get("request_id", _pre_rid),
                        "response_post_id": dup_rec.get("response_post_id", ""),
                        "response_post_ids": dup_rec.get("response_post_ids", [dup_rec.get("response_post_id")] if dup_rec.get("response_post_id") else []),
                        "response_marker": dup_rec.get("response_marker", ""),
                        "acp": {"status": "duplicate_suppressed", "idempotency_key": _k},
                    }
                if _c is not None and not _c.is_duplicate:
                    _idem_key = _c.key
                    _idem_claim = _c
                    _idem_early_claimed = True
                    try:
                        from control_plane.mattermost_policy_gate import _get_audit_ledger as _gal2, _emit_policy_audit as _epa2  # type: ignore
                        _ledger3 = _gal2()
                        if _ledger3 is not None:
                            _epa2(_ledger3, tenant_id=tenant_id, user_id=mapping.human_principal, agent_id=mapping.agent_principal, session_id=_tmp_sid, trace_id=_tmp_trace, request_id=_pre_rid, action="INTERACT", resource=f"session/ingress/{tenant_id}/{_tmp_sid}", decision="ALLOW", policy_version=None, reason=f"idempotency claimed key={_k}")
                    except Exception:
                        pass
        except Exception as _idem_exc:
            from fastapi import HTTPException as _HE2
            if isinstance(_idem_exc, _HE2):
                raise
            import logging as _lg
            _lg.getLogger(__name__).warning("idempotency early claim failed non-prod fallback: %s", _idem_exc)
            # fall through to normal flow (non-prod fallback will proceed)

    # Session: resume the owner's latest durable session when the bridge does
    # not provide one. This is the Mattermost conversation continuity boundary.
    if not session_id:
        try:
            prior = session_store.find_latest_for_owner(tenant_id, mapping.human_principal)
            if prior is not None and prior.status == "active":
                session_id = prior.session_id
        except Exception as exc:
            if os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod"):
                raise HTTPException(status_code=503, detail="durable session lookup unavailable") from exc
            log.warning("latest session lookup failed: %s", exc)
    if session_id:
        try:
            rec = session_store.get(session_id, user_id)
        except (KeyError, PermissionError) as e:
            raise HTTPException(status_code=404 if isinstance(e, KeyError) else 403, detail=str(e))
    else:
        routing = route_session(mapping.security_domain)
        # A안: resolve display_name/avatar_url for this agent before session create
        _dn, _av = _get_personal_display_name(mapping.agent_principal)
        # also try personal_agent display_name from mapping if available via make_profile? fallback to mapped agent_id suffix
        rec = session_store.create(
            tenant_id=tenant_id,
            user_id=mapping.human_principal,
            agent_id=mapping.agent_principal,
            security_domain=mapping.security_domain,
            hermes_worker=routing["pool"],
            display_name=_dn,
            avatar_url=_av,
        )
        session_id = rec.session_id

    # ── Mattermost -> ACP Policy Gate (small-business profile) ──────────
    # Identity/ownership already validated above (map + session_store.get).
    # Now deterministic PolicyEngine evaluation BEFORE any ACP/LLM forwarding.
    # Ordinary conversational prompt is INTERACT low-risk but still audited.
    # _pre_rid already allocated at top for idempotency; reuse for policy audit
    await _evaluate_ingress_policy(
        tenant_id=tenant_id,
        mapping=mapping,
        session_id=session_id,
        trace_id=rec.trace_id,
        request_id=_pre_rid,
        channel_id=channel_id,
    )

    # ── P0 Idempotency gate (durable, CP authoritative) — late path only if early gate was skipped (e.g., no post_id at top or fallback) ──
    if post_id and not _idem_early_claimed:
        # Only run if we did not already claim in early gate (duplicate case already returned)
        # This handles rare case where early gate fell through via exception fallback
        _late_possible = _idem_key is None and _idem_claim is None
        if _late_possible:
            try:
                from control_plane.idempotency import try_claim as _idem_try_claim2, build_idempotency_key as _idem_build2
                _idem_key2 = _idem_build2(tenant_id, channel_id, post_id)
                if _idem_key2 is not None:
                    _claim_res2 = _idem_try_claim2(tenant_id=tenant_id, channel_id=channel_id, post_id=post_id, session_id=session_id, trace_id=rec.trace_id, request_id=_pre_rid)
                    _, _idem_claim2 = _claim_res2
                    if _idem_claim2 is not None and _idem_claim2.is_duplicate:
                        dup_rec2 = _idem_claim2.record
                        try:
                            from control_plane.mattermost_policy_gate import _get_audit_ledger as _gal3, _emit_policy_audit as _epa3  # type: ignore
                            _ledger4 = _gal3()
                            if _ledger4 is not None:
                                _epa3(_ledger4, tenant_id=tenant_id, user_id=mapping.human_principal, agent_id=mapping.agent_principal, session_id=dup_rec2.get("session_id", session_id), trace_id=dup_rec2.get("trace_id", rec.trace_id), request_id=_pre_rid, action="INTERACT", resource=f"session/ingress/{tenant_id}/{session_id}", decision="ALLOW", policy_version=None, reason=f"idempotency duplicate late { _idem_claim2.status} key={_idem_key2} response_post_id={dup_rec2.get('response_post_id','')}")
                        except Exception:
                            pass
                        return {
                            "received": True,
                            "duplicate": True,
                            "idempotency_key": _idem_key2,
                            "idempotency_status": _idem_claim2.status,
                            "session_id": dup_rec2.get("session_id", session_id),
                            "agent_id": mapping.agent_principal,
                            "trace_id": dup_rec2.get("trace_id", rec.trace_id),
                            "request_id": dup_rec2.get("request_id", _pre_rid),
                            "response_post_id": dup_rec2.get("response_post_id", ""),
                            "response_post_ids": dup_rec2.get("response_post_ids", [dup_rec2.get("response_post_id")] if dup_rec2.get("response_post_id") else []),
                            "response_marker": dup_rec2.get("response_marker", ""),
                            "acp": {"status": "duplicate_suppressed", "idempotency_key": _idem_key2},
                        }
                    if _idem_claim2 is not None and not _idem_claim2.is_duplicate:
                        _idem_key = _idem_claim2.key
                        _idem_claim = _idem_claim2
            except Exception as _idem_exc2:
                from fastapi import HTTPException as _HE2b
                if isinstance(_idem_exc2, _HE2b):
                    raise
                import logging as _lg2
                _lg2.getLogger(__name__).warning("idempotency late claim failed non-prod fallback: %s", _idem_exc2)

    # ── Phase 1 MVP: "정리해줘" keyword → demo orchestrator routing ──
    if _is_briefing_request(text):
        run_briefing = _load_orchestrator()
        if run_briefing is not None:
            agent_ctx = {
                "tenant_id": tenant_id,
                "user_id": mapping.human_principal,
                "agent_id": mapping.agent_principal,
                "session_id": session_id,
                "trace_id": rec.trace_id,
                "request_id": new_request_id(),
                "security_domain": mapping.security_domain,
            }
            briefing_result = await run_briefing(agent_ctx, tenant_id)
            rid = new_request_id()
            session_store.append_prompt(session_id, user_id, text, rid)
            # Adaptive Profile: async evidence worker (fire-and-forget, never blocks)
            try:
                from control_plane.adaptive_profile.worker import handle_interaction_event as _ap_handle
                _ap_handle({
                    "tenant_id": tenant_id,
                    "user_id": mapping.human_principal,
                    "session_id": session_id,
                    "conversation_id": session_id,
                    "message_id": rid,
                    "task_type": mapping.security_domain,
                    "text": text,
                })
            except Exception:
                pass
            session_store.append_stream_event(session_id, {"type": "briefing", "data": briefing_result, "trace_id": rec.trace_id})
            # optional: post briefing summary to Mattermost threaded
            if channel_id:
                try:
                    adapter = _get_mattermost_adapter()
                    if adapter is not None:
                        briefing_text = json.dumps(briefing_result.get("briefing", briefing_result), ensure_ascii=False)[:4000]
                        # fire-and-forget threaded post
                        asyncio.create_task(adapter.send_message(channel_id, briefing_text, root_id=post_id))
                except Exception:
                    pass
            return {
                "received": True,
                "routed": "morning-briefing",
                "session_id": session_id,
                "agent_id": mapping.agent_principal,
                "trace_id": rec.trace_id,
                "request_id": rid,
                "briefing": briefing_result.get("briefing"),
                "sources": briefing_result.get("sources"),
                "approvals_required": briefing_result.get("approvals_required"),
                "audit": briefing_result.get("audit"),
                "acp": {"status": "routed_to_briefing"},
            }

    # Forward prompt (non-briefing path)
    rid = new_request_id()
    # multimodal: normalize runtime_context from caller (bridge) + canonical ids
    _rctx = dict(runtime_context) if isinstance(runtime_context, dict) else {}
    # ensure channel/root/post are in runtime_context for ACP trace
    for _k, _v in (("channel_id", channel_id), ("root_id", root_id or post_id), ("post_id", post_id), ("tenant_id", tenant_id), ("user_id", user_id)):
        if _v and _k not in _rctx:
            _rctx[_k] = _v
    _fid = file_ids or []
    _arefs = attachment_refs or []
    # also accept single attachment_ref alias
    if not _arefs and isinstance(runtime_context, dict) and runtime_context.get("attachment_ref"):
        _arefs = [runtime_context["attachment_ref"]]
    if _arefs and not _fid:
        _fid = [r.get("attachment_id") or r.get("vault_path") or r.get("file_id") for r in _arefs if isinstance(r, dict)]
        _fid = [x for x in _fid if x]
    session_store.append_prompt(session_id, user_id, text, rid, file_ids=_fid or None, attachment_refs=_arefs or None, runtime_context=_rctx or None)
    _archive_conversation_turn(tenant_id, mapping.agent_principal, mapping.human_principal, session_id, rid, text)
    # Adaptive Profile: async evidence worker (fire-and-forget, never blocks response path)
    try:
        from control_plane.adaptive_profile.worker import handle_interaction_event as _ap_handle
        _ap_handle({
            "tenant_id": tenant_id,
            "user_id": mapping.human_principal,
            "session_id": session_id,
            "conversation_id": session_id,
            "message_id": rid,
            "task_type": mapping.security_domain,
            "text": text,
        })
    except Exception:
        pass
    acp = ACPAdapter(settings.hermes_base_url)
    try:
        acp_result = await acp.send_prompt(rec, text, rid, attachment_refs=_arefs or None, file_ids=_fid or None, runtime_context=_rctx or None)
    except Exception as _acp_exc:
        if _idem_key:
            try:
                from control_plane.idempotency import fail as _idem_fail2, is_retryable_error as _is_retry2
                _idem_fail2(_idem_key, error=str(_acp_exc)[:500], retryable=_is_retry2(_acp_exc))
            except Exception:
                pass
        raise
    session_store.append_stream_event(session_id, {"type": "prompt_queued", "data": {"text": text, "request_id": rid, "file_ids": _fid, "attachment_refs": _arefs, "runtime_context": _rctx}, "trace_id": rec.trace_id})

    # Streaming: fetch stream and post incremental updates via MattermostAdapter (threaded, root_id)
    if channel_id:
        # background task — don't block response; use thread root if provided
        thread_root = root_id or post_id
        try:
            asyncio.create_task(_stream_and_post_to_mattermost(channel_id, thread_root, rec, idempotency_key=_idem_key))
        except Exception:
            pass

    return {
        "received": True,
        "session_id": session_id,
        "agent_id": mapping.agent_principal,
        "trace_id": rec.trace_id,
        "request_id": rid,
        "idempotency_key": _idem_key or "",
        "acp": acp_result,
    }


@router.post("/mattermost/events")
async def mattermost_event(request: Request, x_signature: str | None = Header(default=None, alias="X-Mattermost-Signature")):
    body = await request.body()
    secret = getattr(settings, "mattermost_webhook_secret", None) or getattr(settings, "mattermost_webhook_secret", "")  # noqa
    # also support MATTERMOST_WEBHOOK_SECRET env via settings
    if not secret:
        secret = getattr(settings, "mattermost_webhook_secret", None)
    if not verify_mattermost_signature(body, x_signature, secret):
        raise HTTPException(status_code=401, detail="invalid mattermost signature")

    try:
        payload: dict[str, Any] = json.loads(body) if body else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON")

    # Expected payload (MVP): {"tenant_id": "...", "user_id": "employee:kim", "text": "...", "channel_id": "...", "session_id": "...?"}
    # Tenant: never trust payload tenant_id — use server-configured tenant (HMAC only proves Mattermost origin, not tenant scope)
    tenant_id: str = _resolve_tenant_id(payload.get("tenant_id"))
    raw_user_id: str = payload.get("user_id") or payload.get("user", {}).get("id", "") or ""
    raw_user_name: str | None = payload.get("user_name") or payload.get("user", {}).get("username") or payload.get("username")
    # Preserve 400 on truly missing identity — do not silently map empty to employee:unknown
    if not (raw_user_id or "").strip() and not (raw_user_name or "").strip():
        user_id: str = ""
    else:
        user_id: str = _resolve_user_id(raw_user_id, raw_user_name)
    text: str = payload.get("text") or payload.get("message") or ""
    session_id: str | None = payload.get("session_id")
    channel_id: str | None = payload.get("channel_id") or payload.get("channel", {}).get("id")
    post_id: str | None = payload.get("post_id") or payload.get("id") or payload.get("data", {}).get("post", {}).get("id")
    root_id: str | None = payload.get("root_id") or payload.get("data", {}).get("post", {}).get("root_id") or payload.get("rootId")

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id (employee:...) required")
    # Allow image-only posts (no text) when file_ids/attachments present — forwarded via Agent Runtime
    _raw_fids = payload.get("file_ids") if isinstance(payload.get("file_ids"), list) else None
    _raw_arefs = payload.get("attachment_refs") or payload.get("attachments") or ([payload.get("attachment_ref")] if payload.get("attachment_ref") else None)
    if isinstance(_raw_arefs, dict):
        _raw_arefs = [_raw_arefs]
    _raw_rctx = payload.get("runtime_context") if isinstance(payload.get("runtime_context"), dict) else {}
    # normalize runtime_context from bridge (already contains channel/root/post)
    if not text and not (_raw_fids or _raw_arefs):
        raise HTTPException(status_code=400, detail="text/message required (or file_ids/attachment_refs for image)")
    # ensure text is at least placeholder for multimodal runtime (ACP builds list)
    if not text and (_raw_fids or _raw_arefs):
        text = payload.get("text") or ""  # allow empty; ACP will handle image-only via multimodal

    return await _handle_core_logic(tenant_id, user_id, text, session_id, channel_id=channel_id, post_id=post_id, root_id=root_id, file_ids=_raw_fids, attachment_refs=_raw_arefs, runtime_context=_raw_rctx)


@router.post("/mattermost/slash")
async def mattermost_slash(request: Request, x_signature: str | None = Header(default=None, alias="X-Mattermost-Signature")):
    """Slash command endpoint — verifies HMAC, parses command/text, reuses same session/briefing logic."""
    body = await request.body()
    secret = getattr(settings, "mattermost_webhook_secret", None)
    if not verify_mattermost_signature(body, x_signature, secret):
        raise HTTPException(status_code=401, detail="invalid mattermost signature")

    content_type = request.headers.get("content-type", "")
    payload: dict[str, Any] = {}
    text = ""
    command = ""
    user_id = ""
    channel_id: str | None = None
    team_id: str | None = None
    session_id: str | None = None
    tenant_id: str = settings.tenant_id

    if "application/x-www-form-urlencoded" in content_type or b"=" in body and b"&" in body:
        # Mattermost slash sends form-urlencoded
        try:
            parsed = urllib.parse.parse_qs(body.decode())
            # parse_qs values are lists
            def _get(k: str) -> str:
                v = parsed.get(k)
                return v[0] if v else ""

            command = _get("command")
            text = _get("text")
            _raw_uid_form = _get("user_id") or _get("user")
            _raw_uname_form = _get("user_name")
            user_id = _resolve_user_id(_raw_uid_form, _raw_uname_form) if _raw_uid_form else ""
            channel_id = _get("channel_id") or None
            team_id = _get("team_id") or None
            session_id = _get("session_id") or None
            _payload_tenant_form = _get("tenant_id")
            tenant_id = _resolve_tenant_id(_payload_tenant_form) if _payload_tenant_form else tenant_id
            # also allow explicit payload json in text? keep text as-is
        except Exception:
            raise HTTPException(status_code=400, detail="invalid form payload")
    else:
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON")
        command = payload.get("command") or ""
        text = payload.get("text") or payload.get("message") or ""
        _raw_uid = payload.get("user_id") or payload.get("user", {}).get("id", "") or ""
        _raw_uname = payload.get("user_name") or payload.get("user", {}).get("username")
        if not (_raw_uid or "").strip() and not (_raw_uname or "").strip():
            user_id = ""
        else:
            user_id = _resolve_user_id(_raw_uid, _raw_uname)
        channel_id = payload.get("channel_id") or payload.get("channel", {}).get("id")
        team_id = payload.get("team_id")
        session_id = payload.get("session_id")
        _payload_tenant_json = payload.get("tenant_id")
        tenant_id = _resolve_tenant_id(_payload_tenant_json) if _payload_tenant_json else tenant_id

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if not text and not command:
        raise HTTPException(status_code=400, detail="text/command required")

    # If text empty but command provided, use command as text
    effective_text = text or command
    # Optionally prefix command for audit
    if command and text:
        effective_text = f"{command} {text}"

    return await _handle_core_logic(tenant_id, user_id, effective_text, session_id, channel_id=channel_id, post_id=None)


@router.post("/mattermost/actions")
async def mattermost_actions(request: Request, x_signature: str | None = Header(default=None, alias="X-Mattermost-Signature")):
    """Interactive action endpoint — handles Approval 4-button payload.

    Expected payload (Mattermost interactive message):
      {
        "user_id": "employee:kim" or mattermost user id,
        "user_name": "...",
        "channel_id": "...",
        "post_id": "...",
        "context": {"approval_id": "apr_xxx", "decision": "APPROVED_ONCE"},
        "context_decision": "APPROVED_ONCE"  # alternative flat
      }
    Also supports: {"approval_id": "...", "decision": "...", "user_id": "..."}
    Maps decision to ApprovalDecision and calls security approval service.
    """
    body = await request.body()
    secret = getattr(settings, "mattermost_webhook_secret", None)
    if not verify_mattermost_signature(body, x_signature, secret):
        raise HTTPException(status_code=401, detail="invalid mattermost signature")

    # Mattermost interactive actions may send application/x-www-form-urlencoded with payload=JSON
    content_type = request.headers.get("content-type", "")
    payload: dict[str, Any] = {}
    if "application/x-www-form-urlencoded" in content_type:
        try:
            parsed = urllib.parse.parse_qs(body.decode())
            # Mattermost sends 'payload' as JSON string
            raw_payload = parsed.get("payload", [None])[0]
            if raw_payload:
                payload = json.loads(raw_payload)
            else:
                # fallback: flatten qs
                payload = {k: v[0] for k, v in parsed.items()}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON")
        except Exception:
            raise HTTPException(status_code=400, detail="invalid form payload")
    else:
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON")

    # Extract fields — support multiple shapes
    context = payload.get("context") or {}
    # Mattermost sends context inside integration context
    approval_id = context.get("approval_id") or payload.get("approval_id") or ""
    decision = context.get("decision") or payload.get("decision") or payload.get("action") or ""
    # Normalize decision (Mattermost action id -> decision)
    decision_map = {
        "deny": "DENIED",
        "approve_once": "APPROVED_ONCE",
        "approve_user_always": "APPROVED_USER_ALWAYS",
        "approve_group_always": "APPROVED_GROUP_ALWAYS",
    }
    if decision in decision_map:
        decision = decision_map[decision]
    decision = decision.upper() if isinstance(decision, str) else ""

    user_id = payload.get("user_id") or payload.get("user", {}).get("id", "") or ""
    user_name = payload.get("user_name") or payload.get("user", {}).get("username", "") or ""
    channel_id = payload.get("channel_id") or ""
    post_id = payload.get("post_id") or ""

    if not approval_id:
        raise HTTPException(status_code=400, detail="approval_id required")
    if decision not in VALID_DECISIONS:
        raise HTTPException(status_code=400, detail=f"invalid decision: {decision}, must be one of {VALID_DECISIONS}")

    # Map Mattermost user to employee principal for decided_by
    decided_by = user_id
    # Try mattermost adapter mapping
    try:
        adapter = _get_mattermost_adapter()
        if adapter is not None and user_id:
            decided_by = adapter.map_mattermost_user(user_id, user_name)
    except Exception:
        pass

    # Call approval service
    store = _get_approval_store()
    if store is None:
        raise HTTPException(status_code=500, detail="approval service unavailable")

    # If approval not found in this store instance, try to synthesize minimal request for test/dev
    # In prod, approvals are persisted in DB/Redis — here we support in-memory for tests
    try:
        from approval_workflow.workflow import ApprovalDecision  # type: ignore

        # Ensure request exists for idempotency in tests: if not found, return 404
        existing = store.get(approval_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"approval not found: {approval_id}")

        decision_enum = ApprovalDecision(decision)
        group_id = payload.get("group_id") or context.get("group_id") or None
        result = store.decide(approval_id, decision_enum, decided_by, group_id=group_id)
        # Post confirmation back to Mattermost threaded if channel available
        if channel_id:
            try:
                adapter = _get_mattermost_adapter()
                if adapter is not None:
                    asyncio.create_task(adapter.send_message(channel_id, f"Approval {approval_id} → {decision} by {decided_by}", root_id=post_id or None))
            except Exception:
                pass
        return {"approval_id": approval_id, "decision": decision, "decided_by": decided_by, "status": result.decision.value if hasattr(result.decision, "value") else str(result.decision)}
    except HTTPException:
        raise
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get("/mattermost/health")
def mm_health():
    return {"status": "ok", "adapter": "mattermost", "workstream": "A"}
