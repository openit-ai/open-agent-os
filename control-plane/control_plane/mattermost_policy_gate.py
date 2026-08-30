"""Mattermost -> ACP -> Policy Engine gate — deterministic small-business profile.

Scope (task §1-2):
  * Deterministic policy profile (small_business_bundle) with explicit DENY override.
  * Separates User Permission Level (authenticated/owned identity) from Task Risk (LOW/MEDIUM/HIGH/CRITICAL).
  * Default DENY — ALLOW only for:
      - authenticated/owned Mattermost Personal Agent conversational ingress (INTERACT on session/ingress/* or mattermost/ingress/*)
      - authorized read-only Outline/company knowledge (READ/SEARCH on outline/*) + owned personal read
  * Require APPROVAL_REQUIRED or DENY for writes, external sends, merge/deploy/delete/export.
  * Every ingress (including low-risk INTERACT) produces a POLICY_DECISION audit event.
  * Fail-closed on policy engine error or audit persistence error.
  * Reuses existing PolicyEngine / AuthorizationHook / AuditLedger semantics (no bypass).

This module is the single ingress gate for webhook.py. It wraps AuthorizationHook
(enterprise/personal branching) + small_business_bundle PolicyEngine + AuditLedger.
If AuthorizationHook is unavailable, it falls back to direct PolicyEngine evaluation
with identical decision semantics but still fail-closed.

Determinism: no LLM, no network, pure fnmatch + ordered evaluation.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "security" / "policy-engine"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages" / "policy-model"))
    from policy_engine.small_business_bundle import classify_risk, TASK_RISK, PERMISSION_LEVELS  # type: ignore
except Exception:  # pragma: no cover
    classify_risk = None  # type: ignore
    TASK_RISK = {}  # type: ignore
    PERMISSION_LEVELS = {}  # type: ignore

def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        if os.getenv(k, "").strip().lower() in ("production", "prod"):
            return True
    return False

def _preserve_audit_evidence(*, tenant_id: str, user_id: str, agent_id: str, session_id: str, trace_id: str, request_id: str, action: str, resource: str, decision: str, reason: str, audit_error: Exception) -> None:
    try:
        import logging as _logging
        _logging.getLogger(__name__).error("audit fail-closed tenant=%s user=%s decision=%s audit_error=%s reason=%s", tenant_id, user_id, decision, audit_error, reason)
    except Exception:
        pass
    try:
        import sys as _sys
        print(f"AUDIT_FAIL tenant={tenant_id} user={user_id} decision={decision} audit_error={audit_error} reason={reason}", file=_sys.stderr)
    except Exception:
        pass
    try:
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        from pathlib import Path as _Path
        rec = {"ts": _dt.now(_tz.utc).isoformat(), "tenant_id": tenant_id, "user_id": user_id, "agent_id": agent_id, "session_id": session_id, "trace_id": trace_id, "request_id": request_id, "action": action, "resource": resource, "decision": decision, "reason": reason, "audit_error": str(audit_error)}
        with open("/tmp/oaos_audit_fail.log", "a") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

_INGRESS_ACTION = "INTERACT"
def _ingress_resource(tenant_id: str, session_id: str) -> str:
    import re
    safe = lambda v: re.sub(r"[^a-zA-Z0-9._-]", "_", str(v))[:64] or "default"
    return f"session/ingress/{safe(tenant_id)}/{safe(session_id)}"

def _load_active_published_bundle(tenant_id: str):
    """Load active published admin policy bundle if present via shared read-only loader.

    - When a published row exists, returns PolicyBundle constructed from its rules.
    - When no published row, returns None (caller falls back to small_business_bundle).
    - Avoids importing admin-console module directly; uses shared active_policy_loader.
    - In production, DB errors raise RuntimeError so caller can fail-closed; non-prod falls back.
    """
    ROOT = Path(__file__).resolve().parents[2]
    for p in [ROOT / "security" / "policy-engine", ROOT / "packages" / "policy-model"]:
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    try:
        from policy_engine.active_policy_loader import get_active_published_bundle  # type: ignore
        return get_active_published_bundle(tenant_id)
    except RuntimeError:
        # production DB error — propagate for fail-closed
        raise
    except Exception:
        return None

def _get_small_business_engine(tenant_id: str):
    ROOT = Path(__file__).resolve().parents[2]
    for p in [ROOT / "security" / "policy-engine", ROOT / "packages" / "policy-model"]:
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    # Priority 1: active published admin bundle (UI)
    try:
        active_bundle = _load_active_published_bundle(tenant_id)
        if active_bundle is not None:
            from policy_engine.engine import PolicyEngine  # type: ignore
            return PolicyEngine([active_bundle])
    except RuntimeError as e:
        # Production DB error — fail-closed: no engine => gate will DENY
        if _is_production():
            import logging as _lg
            _lg.getLogger(__name__).error("published policy load failed in production — fail-closed: %s", e)
            return None
        # non-prod: fall through to default bundle
        pass
    except Exception:
        pass
    try:
        from policy_engine.small_business_bundle import small_business_bundle  # type: ignore
        from policy_engine.engine import PolicyEngine  # type: ignore
        return PolicyEngine([small_business_bundle(tenant_id)])
    except Exception:
        try:
            from policy_engine.default_bundle import default_bundle  # type: ignore
            from policy_engine.engine import PolicyEngine  # type: ignore
            return PolicyEngine([default_bundle(tenant_id)])
        except Exception:
            return None

def _get_authorization_hook(tenant_id: str):
    ROOT = Path(__file__).resolve().parents[2]
    for p in [ROOT / "execution-gateway", ROOT / "security" / "policy-engine", ROOT / "packages" / "policy-model"]:
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    try:
        from execution_gateway.authz_hook import AuthorizationHook  # type: ignore
        engine = _get_small_business_engine(tenant_id)
        try:
            return AuthorizationHook(policy_engine=engine, tenant_id=tenant_id)
        except Exception:
            return AuthorizationHook(tenant_id=tenant_id)
    except Exception:
        return None

def _get_audit_ledger():
    ROOT = Path(__file__).resolve().parents[2]
    for p in [ROOT / "security" / "audit", ROOT / "packages" / "audit-model", ROOT / "security"]:
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    try:
        from audit.audit_ledger.ledger import AuditLedger  # type: ignore
        from control_plane.config import settings  # type: ignore
        key = getattr(settings, "mattermost_webhook_secret", "") or os.getenv("OAOS_AUDIT_SIGNING_KEY", "") or "dev-audit-key"
        return AuditLedger(signing_key=key)
    except Exception:
        try:
            from audit.audit_ledger.ledger import AuditLedger  # type: ignore
            return AuditLedger(signing_key=os.getenv("OAOS_AUDIT_SIGNING_KEY", "dev-audit-key"))
        except Exception:
            return None

def _emit_policy_audit(
    ledger: Any,
    *,
    tenant_id: str,
    user_id: str,
    agent_id: str,
    session_id: str,
    trace_id: str,
    request_id: str,
    action: str,
    resource: str,
    decision: str,
    policy_version: str | None,
    reason: str,
) -> Any | None:
    if ledger is None:
        raise RuntimeError("audit ledger unavailable — fail-closed")
    try:
        ROOT = Path(__file__).resolve().parents[2]
        for p in [ROOT / "packages" / "audit-model"]:
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
        from audit_model import AuditEvent, AuditEventType  # type: ignore
        from datetime import datetime, timezone
        import uuid as _uuid
        evt = AuditEvent(
            event_id=f"evt_{_uuid.uuid4().hex[:12]}",
            event_type=AuditEventType.POLICY_DECISION,
            timestamp=datetime.now(timezone.utc),
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            trace_id=trace_id,
            request_id=request_id,
            resource=resource,
            action=action,
            decision=decision,
            policy_version=policy_version,
        )
        try:
            import hashlib as _hl
            evt.result_hash = _hl.sha256(reason.encode()).hexdigest()[:16]
        except Exception:
            pass
        ledger.append(evt)
        return evt
    except Exception as e:
        raise RuntimeError(f"audit emit failed: {e}") from e

class MattermostPolicyGate:
    def __init__(self, tenant_id: str = "default") -> None:
        self.tenant_id = tenant_id
        self._engine = _get_small_business_engine(tenant_id)
        self._hook = _get_authorization_hook(tenant_id)
        self._ledger = _get_audit_ledger()

    def _resolve_engine(self, tenant_id: str | None = None):
        tid = tenant_id or self.tenant_id
        # Only refresh from active published admin bundle; fallback path must not mask fail-closed injection (production test)
        # In production, DB errors propagate as RuntimeError — refresh should fail-closed (return None engine state observed by caller via hook)
        try:
            active_bundle = _load_active_published_bundle(tid)
            if active_bundle is not None:
                from policy_engine.engine import PolicyEngine  # type: ignore
                eng = PolicyEngine([active_bundle])
                self._engine = eng
                try:
                    if self._hook is not None and hasattr(self._hook, "engine"):
                        self._hook.engine = eng
                except Exception:
                    pass
                return self._engine
        except RuntimeError as e:
            if _is_production():
                # production DB error: mark engine unavailable so gate will DENY (fail-closed)
                import logging as _lg
                _lg.getLogger(__name__).error("published policy refresh failed in production — fail-closed: %s", e)
                # do not fallback to small_business; keep engine as None to trigger fail-closed
                # but we must signal via hook engine None if present
                try:
                    if self._hook is not None and hasattr(self._hook, "engine"):
                        self._hook.engine = None
                except Exception:
                    pass
                self._engine = None
                return self._engine
            pass
        except Exception:
            pass
        return self._engine

    async def authorize_ingress(
        self,
        mapping: Any,
        session_id: str,
        trace_id: str,
        request_id: str,
        channel_id: str | None = None,
        *,
        action: str | None = None,
        resource: str | None = None,
    ) -> Any:
        tenant_id = getattr(mapping, "tenant_id", self.tenant_id) or self.tenant_id
        user_id = getattr(mapping, "human_principal", getattr(mapping, "user_id", ""))
        agent_id = getattr(mapping, "agent_principal", getattr(mapping, "agent_id", ""))
        act = action or _INGRESS_ACTION
        res = resource or _ingress_resource(tenant_id, session_id)
        # Refresh active published bundle on each authorize so UI publish takes effect immediately (preserve fallback)
        try:
            self._resolve_engine(tenant_id)
        except Exception:
            pass
        # Fail-closed before authorize if hook.engine is None: do not allow permissive fallback in production
        if self._hook is not None and getattr(self._hook, "engine", None) is None:
            if _is_production():
                decision = "DENY"
                reason = "policy engine unavailable via hook — fail-closed before authorize"
                try:
                    _emit_policy_audit(self._ledger, tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, session_id=session_id, trace_id=trace_id, request_id=request_id, action=act, resource=res, decision=decision, policy_version=None, reason=reason)
                except Exception as ae:
                    _preserve_audit_evidence(tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, session_id=session_id, trace_id=trace_id, request_id=request_id, action=act, resource=res, decision=decision, reason=reason, audit_error=ae)
                    from fastapi import HTTPException as _HTTPEx  # type: ignore
                    raise _HTTPEx(status_code=403, detail=f"policy denied: {reason} [audit fail-closed: {ae}]") from ae
                from fastapi import HTTPException as _HTTPEx  # type: ignore
                raise _HTTPEx(status_code=403, detail=f"policy denied: {reason}")
            # non-prod: fall through to hook's own test fallback (do not fail here)
        if self._hook is not None:
            agent_ctx = {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "trace_id": trace_id,
                "request_id": request_id,
            }
            extra = {"channel_id": channel_id or "", "ingress": "mattermost"}
            try:
                authz = await self._hook.authorize(agent_ctx, action=act, resource=res, extra_context=extra)
            except Exception as e:
                decision = "DENY"
                reason = f"authorization hook error: {e}"
                try:
                    _emit_policy_audit(self._ledger, tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, session_id=session_id, trace_id=trace_id, request_id=request_id, action=act, resource=res, decision=decision, policy_version=None, reason=reason)
                except Exception as audit_e:
                    _preserve_audit_evidence(tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, session_id=session_id, trace_id=trace_id, request_id=request_id, action=act, resource=res, decision=decision, reason=reason, audit_error=audit_e)
                    from fastapi import HTTPException as _HTTPEx  # type: ignore
                    raise _HTTPEx(status_code=403, detail=f"policy denied: audit fail-closed: {audit_e} (orig: {reason})") from audit_e
                from fastapi import HTTPException as _HTTPEx  # type: ignore
                raise _HTTPEx(status_code=403, detail=f"policy denied: {reason}")
            decision = getattr(authz, "decision", "DENY")
            reason = getattr(authz, "reason", "")
            source = getattr(authz, "source", "")
            policy_version = getattr(authz, "matched_rule_id", None)
            try:
                _emit_policy_audit(self._ledger, tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, session_id=session_id, trace_id=trace_id, request_id=request_id, action=act, resource=res, decision=decision, policy_version=policy_version, reason=f"{reason} source={source}")
            except Exception as e:
                _preserve_audit_evidence(tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, session_id=session_id, trace_id=trace_id, request_id=request_id, action=act, resource=res, decision=decision, reason=f"{reason} source={source}", audit_error=e)
                from fastapi import HTTPException as _HTTPEx  # type: ignore
                raise _HTTPEx(status_code=403, detail=f"policy denied: audit fail-closed: {e}")
            if decision == "DENY":
                from fastapi import HTTPException as _HTTPEx  # type: ignore
                raise _HTTPEx(status_code=403, detail=f"policy denied: {reason} (source={source})")
            if decision == "APPROVAL_REQUIRED":
                from fastapi import HTTPException as _HTTPEx  # type: ignore
                raise _HTTPEx(status_code=403, detail=f"approval required: {reason} (source={source})")
            return authz
        if self._engine is None:
            decision = "DENY"
            reason = "no policy engine available — fail-closed"
            try:
                _emit_policy_audit(self._ledger, tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, session_id=session_id, trace_id=trace_id, request_id=request_id, action=act, resource=res, decision=decision, policy_version=None, reason=reason)
            except Exception as audit_e:
                _preserve_audit_evidence(tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, session_id=session_id, trace_id=trace_id, request_id=request_id, action=act, resource=res, decision=decision, reason=reason, audit_error=audit_e)
                from fastapi import HTTPException as _HTTPEx  # type: ignore
                raise _HTTPEx(status_code=403, detail=f"policy denied: audit fail-closed: {audit_e} (orig: {reason})") from audit_e
            from fastapi import HTTPException as _HTTPEx  # type: ignore
            raise _HTTPEx(status_code=403, detail=f"policy denied: {reason}")
        try:
            ROOT = Path(__file__).resolve().parents[2]
            for p in [ROOT / "packages" / "policy-model"]:
                if str(p) not in sys.path:
                    sys.path.insert(0, str(p))
            from policy_model import PolicyEvaluationRequest  # type: ignore
            req = PolicyEvaluationRequest(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                action=act,
                resource=res,
                context={"channel_id": channel_id or "", "ingress": "mattermost"},
            )
            result = self._engine.evaluate(req)
            decision = result.decision.value if hasattr(result.decision, "value") else str(result.decision)
            reason = result.reason
            pv = result.matched_rule.id if result.matched_rule else None
            source = result.source.value if hasattr(result.source, "value") else str(result.source)
            try:
                _emit_policy_audit(self._ledger, tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, session_id=session_id, trace_id=trace_id, request_id=request_id, action=act, resource=res, decision=decision, policy_version=pv, reason=f"{reason} source={source}")
            except Exception as e:
                _preserve_audit_evidence(tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, session_id=session_id, trace_id=trace_id, request_id=request_id, action=act, resource=res, decision=decision, reason=f"{reason} source={source}", audit_error=e)
                from fastapi import HTTPException as _HTTPEx  # type: ignore
                raise _HTTPEx(status_code=403, detail=f"policy denied: audit fail-closed: {e}")
            if decision == "DENY":
                from fastapi import HTTPException as _HTTPEx  # type: ignore
                raise _HTTPEx(status_code=403, detail=f"policy denied: {reason} (source={source})")
            if decision == "APPROVAL_REQUIRED":
                from fastapi import HTTPException as _HTTPEx  # type: ignore
                raise _HTTPEx(status_code=403, detail=f"approval required: {reason} (source={source})")
            from types import SimpleNamespace as _NS
            return _NS(allowed=True, decision=decision, reason=reason, source=source, matched_rule_id=pv)
        except Exception as e:
            try:
                from fastapi import HTTPException as _HTTPEx  # type: ignore
                if isinstance(e, _HTTPEx):
                    raise
            except Exception:
                pass
            decision = "DENY"
            reason = f"policy engine error: {e}"
            try:
                _emit_policy_audit(self._ledger, tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, session_id=session_id, trace_id=trace_id, request_id=request_id, action=act, resource=res, decision=decision, policy_version=None, reason=reason)
            except Exception as audit_e:
                _preserve_audit_evidence(tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, session_id=session_id, trace_id=trace_id, request_id=request_id, action=act, resource=res, decision=decision, reason=reason, audit_error=audit_e)
                from fastapi import HTTPException as _HTTPEx  # type: ignore
                raise _HTTPEx(status_code=403, detail=f"policy denied: audit fail-closed: {audit_e} (orig: {reason})") from audit_e
            from fastapi import HTTPException as _HTTPEx  # type: ignore
            raise _HTTPEx(status_code=403, detail=f"policy denied: {reason}")

_GATE_CACHE: dict[str, MattermostPolicyGate] = {}
def get_mattermost_gate(tenant_id: str = "default") -> MattermostPolicyGate:
    cached = _GATE_CACHE.get(tenant_id)
    if cached is not None and cached._engine is not None:
        try:
            cached._resolve_engine(tenant_id)
        except Exception:
            pass
        return cached
    gate = MattermostPolicyGate(tenant_id)
    _GATE_CACHE[tenant_id] = gate
    return gate

def clear_mattermost_gate_cache() -> None:
    _GATE_CACHE.clear()
