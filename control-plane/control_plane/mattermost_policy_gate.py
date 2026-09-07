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
import logging
from pathlib import Path
from typing import Any, NoReturn

from fastapi import HTTPException

logger = logging.getLogger(__name__)


class PolicyGateBackendError(RuntimeError):
    """A policy, identity-adjacent, or audit backend cannot serve the request."""


class PolicyGateValidationError(ValueError):
    """The policy request or active policy data is malformed."""


_BACKEND_ERRORS = (
    ConnectionError,
    OSError,
    TimeoutError,
    RuntimeError,
    ImportError,
    ModuleNotFoundError,
)
_VALIDATION_ERRORS = (AttributeError, KeyError, TypeError, ValueError)

try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "security" / "policy-engine"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages" / "policy-model"))
    from policy_engine.small_business_bundle import classify_risk, TASK_RISK, PERMISSION_LEVELS  # type: ignore
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    classify_risk = None  # type: ignore
    TASK_RISK = {}  # type: ignore
    PERMISSION_LEVELS = {}  # type: ignore

def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        if os.getenv(k, "").strip().lower() in ("production", "prod"):
            return True
    return False


def _raise_backend_unavailable(operation: str, exc: BaseException) -> NoReturn:
    raise HTTPException(status_code=503, detail=f"Mattermost {operation} unavailable") from exc


def _validate_ingress_context(mapping: Any, tenant_id: str, session_id: str, trace_id: str, request_id: str) -> None:
    if not tenant_id or not isinstance(tenant_id, str):
        raise HTTPException(status_code=401, detail="missing Mattermost tenant identity")
    if not getattr(mapping, "human_principal", None) or not getattr(mapping, "agent_principal", None):
        raise HTTPException(status_code=401, detail="missing Mattermost identity context")
    if not all(isinstance(value, str) and value.strip() for value in (session_id, trace_id, request_id)):
        raise HTTPException(status_code=422, detail="session_id, trace_id, and request_id are required strings")


def _validate_action_resource(action: str | None, resource: str | None) -> None:
    if action is not None and (not isinstance(action, str) or not action.strip()):
        raise HTTPException(status_code=422, detail="policy action must be a non-empty string")
    if resource is not None and (not isinstance(resource, str) or not resource.strip()):
        raise HTTPException(status_code=422, detail="policy resource must be a non-empty string")

def _preserve_audit_evidence(*, tenant_id: str, user_id: str, agent_id: str, session_id: str, trace_id: str, request_id: str, action: str, resource: str, decision: str, reason: str, audit_error: Exception) -> None:
    try:
        import logging as _logging
        _logging.getLogger(__name__).error("audit fail-closed tenant=%s user=%s decision=%s audit_error=%s reason=%s", tenant_id, user_id, decision, audit_error, reason)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        sys.stderr.write(f"AUDIT_LOG_FAILURE error={exc!r} original={audit_error!r}\n")
    try:
        import sys as _sys
        print(f"AUDIT_FAIL tenant={tenant_id} user={user_id} decision={decision} audit_error={audit_error} reason={reason}", file=_sys.stderr)
    except (OSError, UnicodeError) as exc:
        logger.warning("audit evidence stderr write failed: %s", type(exc).__name__)
    try:
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        from pathlib import Path as _Path
        rec = {"ts": _dt.now(_tz.utc).isoformat(), "tenant_id": tenant_id, "user_id": user_id, "agent_id": agent_id, "session_id": session_id, "trace_id": trace_id, "request_id": request_id, "action": action, "resource": resource, "decision": decision, "reason": reason, "audit_error": str(audit_error)}
        with open("/tmp/oaos_audit_fail.log", "a") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        logger.warning("audit evidence file write failed: %s", type(exc).__name__)

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
    except (ImportError, ModuleNotFoundError) as exc:
        logger.warning("active policy loader unavailable: %s", type(exc).__name__)
        return None
    except _VALIDATION_ERRORS + (OSError,) as exc:
        raise PolicyGateBackendError("active policy loader failed") from exc

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
    except RuntimeError as exc:
        if _is_production():
            logger.error("published policy load failed in production — fail-closed: %s", exc)
            raise PolicyGateBackendError("published policy backend unavailable") from exc
        logger.warning("published policy load degraded in non-production: %s", type(exc).__name__)
    except (ImportError, ModuleNotFoundError) as exc:
        if _is_production():
            raise PolicyGateBackendError("published policy loader unavailable") from exc
        logger.warning("published policy loader unavailable in non-production: %s", type(exc).__name__)
    except _VALIDATION_ERRORS + (OSError,) as exc:
        raise PolicyGateBackendError("published policy load failed") from exc
    try:
        from policy_engine.small_business_bundle import small_business_bundle  # type: ignore
        from policy_engine.engine import PolicyEngine  # type: ignore
        return PolicyEngine([small_business_bundle(tenant_id)])
    except (ImportError, ModuleNotFoundError) as exc:
        if _is_production():
            raise PolicyGateBackendError("policy engine unavailable") from exc
        logger.warning("small-business policy bundle unavailable: %s", type(exc).__name__)
        try:
            from policy_engine.default_bundle import default_bundle  # type: ignore
            from policy_engine.engine import PolicyEngine  # type: ignore
            return PolicyEngine([default_bundle(tenant_id)])
        except (ImportError, ModuleNotFoundError) as fallback_exc:
            logger.warning("default policy bundle unavailable: %s", type(fallback_exc).__name__)
            return None
        except _VALIDATION_ERRORS + (OSError,) as fallback_exc:
            raise PolicyGateBackendError("default policy bundle failed") from fallback_exc
    except _VALIDATION_ERRORS + (OSError,) as exc:
        raise PolicyGateBackendError("small-business policy bundle failed") from exc

def _get_authorization_hook(tenant_id: str):
    ROOT = Path(__file__).resolve().parents[2]
    for p in [ROOT / "execution-gateway", ROOT / "security" / "policy-engine", ROOT / "packages" / "policy-model"]:
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    try:
        from execution_gateway.authz_hook import AuthorizationHook  # type: ignore
    except (ImportError, ModuleNotFoundError) as exc:
        logger.warning("authorization hook unavailable; using direct policy engine: %s", type(exc).__name__)
        return None
    engine = _get_small_business_engine(tenant_id)
    try:
        return AuthorizationHook(policy_engine=engine, tenant_id=tenant_id)
    except _VALIDATION_ERRORS + (OSError,) as exc:
        raise PolicyGateBackendError("authorization hook initialization failed") from exc

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
    except _BACKEND_ERRORS + _VALIDATION_ERRORS as exc:
        logger.warning("primary audit ledger unavailable: %s", type(exc).__name__)
        try:
            from audit.audit_ledger.ledger import AuditLedger  # type: ignore
            return AuditLedger(signing_key=os.getenv("OAOS_AUDIT_SIGNING_KEY", "dev-audit-key"))
        except _BACKEND_ERRORS + _VALIDATION_ERRORS as fallback_exc:
            if _is_production():
                raise PolicyGateBackendError("audit ledger unavailable in production") from fallback_exc
            logger.warning("audit ledger degraded in non-production: %s", type(fallback_exc).__name__)
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
        import hashlib as _hl
        evt.result_hash = _hl.sha256(reason.encode()).hexdigest()[:16]
        ledger.append(evt)
        return evt
    except _BACKEND_ERRORS + _VALIDATION_ERRORS as exc:
        raise PolicyGateBackendError(f"audit emit failed: {exc}") from exc

class MattermostPolicyGate:
    def __init__(self, tenant_id: str = "default") -> None:
        self.tenant_id = tenant_id
        self._engine = _get_small_business_engine(tenant_id)
        self._hook = _get_authorization_hook(tenant_id)
        self._ledger = _get_audit_ledger()

    def _set_hook_engine(self, engine: Any | None) -> None:
        if self._hook is not None and hasattr(self._hook, "engine"):
            try:
                self._hook.engine = engine
            except (AttributeError, TypeError) as exc:
                logger.warning("policy hook engine state update failed: %s", type(exc).__name__)

    def _audit_decision(
        self,
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
        failure_status: int = 403,
    ) -> None:
        try:
            _emit_policy_audit(
                self._ledger,
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                trace_id=trace_id,
                request_id=request_id,
                action=action,
                resource=resource,
                decision=decision,
                policy_version=policy_version,
                reason=reason,
            )
        except _BACKEND_ERRORS + _VALIDATION_ERRORS as audit_error:
            _preserve_audit_evidence(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                trace_id=trace_id,
                request_id=request_id,
                action=action,
                resource=resource,
                decision=decision,
                reason=reason,
                audit_error=audit_error,
            )
            raise HTTPException(
                status_code=failure_status,
                detail=f"policy denied: audit fail-closed: {audit_error}",
            ) from audit_error

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
                self._set_hook_engine(eng)
                return self._engine
        except RuntimeError as exc:
            if _is_production():
                # production DB error: mark engine unavailable so gate will DENY (fail-closed)
                logger.error("published policy refresh failed in production — fail-closed: %s", exc)
                # do not fallback to small_business; keep engine as None to trigger fail-closed
                # but we must signal via hook engine None if present
                self._set_hook_engine(None)
                self._engine = None
                return self._engine
            logger.warning("published policy refresh degraded in non-production: %s", type(exc).__name__)
        except (ImportError, ModuleNotFoundError, OSError, TypeError, ValueError, AttributeError, KeyError) as exc:
            if _is_production():
                self._set_hook_engine(None)
                self._engine = None
                raise PolicyGateBackendError("published policy refresh unavailable") from exc
            logger.warning("published policy refresh degraded in non-production: %s", type(exc).__name__)
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
        tenant_id = getattr(mapping, "tenant_id", None)
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise HTTPException(status_code=401, detail="missing Mattermost tenant identity")
        if tenant_id != self.tenant_id:
            raise HTTPException(status_code=403, detail="Mattermost tenant context mismatch")
        user_id = getattr(mapping, "human_principal", getattr(mapping, "user_id", ""))
        agent_id = getattr(mapping, "agent_principal", getattr(mapping, "agent_id", ""))
        _validate_ingress_context(mapping, tenant_id, session_id, trace_id, request_id)
        _validate_action_resource(action, resource)
        act = action or _INGRESS_ACTION
        res = resource or _ingress_resource(tenant_id, session_id)
        # Refresh active published bundle on each authorize so UI publish takes effect immediately (preserve fallback)
        self._resolve_engine(tenant_id)
        # Fail-closed before authorize if hook.engine is None: do not allow permissive fallback in production
        if self._hook is not None and getattr(self._hook, "engine", None) is None:
            if _is_production():
                reason = "policy engine unavailable via hook — fail-closed before authorize"
                self._audit_decision(
                    tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
                    session_id=session_id, trace_id=trace_id, request_id=request_id,
                    action=act, resource=res, decision="DENY", policy_version=None,
                    reason=reason,
                )
                raise HTTPException(status_code=403, detail=f"policy denied: {reason}")
            logger.warning("policy hook engine unavailable in non-production; continuing with direct policy path")
        if self._hook is not None and getattr(self._hook, "engine", None) is not None:
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
            except HTTPException:
                raise
            except _BACKEND_ERRORS as exc:
                reason = f"authorization backend unavailable: {exc}"
                self._audit_decision(
                    tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
                    session_id=session_id, trace_id=trace_id, request_id=request_id,
                    action=act, resource=res, decision="DENY", policy_version=None,
                    reason=reason, failure_status=503,
                )
                _raise_backend_unavailable("policy evaluation", exc)
            except _VALIDATION_ERRORS as exc:
                raise HTTPException(status_code=422, detail=f"malformed policy context: {exc}") from exc
            decision = getattr(authz, "decision", "DENY")
            decision = decision.value if hasattr(decision, "value") else str(decision)
            reason = str(getattr(authz, "reason", ""))
            source = getattr(authz, "source", "")
            source = source.value if hasattr(source, "value") else str(source)
            policy_version = getattr(authz, "matched_rule_id", None)
            if decision not in {"ALLOW", "DENY", "APPROVAL_REQUIRED"}:
                raise HTTPException(status_code=503, detail="policy evaluator returned an invalid decision")
            audit_reason = f"{reason} source={source}"
            self._audit_decision(
                tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
                session_id=session_id, trace_id=trace_id, request_id=request_id,
                action=act, resource=res, decision=decision, policy_version=policy_version,
                reason=audit_reason,
            )
            if source == "validation":
                raise HTTPException(status_code=422, detail=f"malformed policy request: {reason}")
            if decision == "DENY":
                raise HTTPException(status_code=403, detail=f"policy denied: {reason} (source={source})")
            if decision == "APPROVAL_REQUIRED":
                raise HTTPException(status_code=403, detail=f"approval required: {reason} (source={source})")
            return authz
        if self._engine is None:
            reason = "no policy engine available — fail-closed"
            self._audit_decision(
                tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
                session_id=session_id, trace_id=trace_id, request_id=request_id,
                action=act, resource=res, decision="DENY", policy_version=None,
                reason=reason,
            )
            raise HTTPException(status_code=403, detail=f"policy denied: {reason}")
        ROOT = Path(__file__).resolve().parents[2]
        for p in [ROOT / "packages" / "policy-model"]:
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
        try:
            from policy_model import PolicyEvaluationRequest  # type: ignore
            req = PolicyEvaluationRequest(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                action=act,
                resource=res,
                context={"channel_id": channel_id or "", "ingress": "mattermost"},
            )
        except (ImportError, ModuleNotFoundError) as exc:
            _raise_backend_unavailable("policy evaluator", exc)
        except _VALIDATION_ERRORS as exc:
            raise HTTPException(status_code=422, detail=f"malformed policy request: {exc}") from exc
        try:
            result = self._engine.evaluate(req)
        except _BACKEND_ERRORS as exc:
            reason = f"policy evaluator unavailable: {exc}"
            self._audit_decision(
                tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
                session_id=session_id, trace_id=trace_id, request_id=request_id,
                action=act, resource=res, decision="DENY", policy_version=None,
                reason=reason, failure_status=503,
            )
            _raise_backend_unavailable("policy evaluator", exc)
        except _VALIDATION_ERRORS as exc:
            raise HTTPException(status_code=422, detail=f"malformed policy response: {exc}") from exc
        try:
            decision = result.decision.value if hasattr(result.decision, "value") else str(result.decision)
            reason = str(result.reason)
            pv = result.matched_rule.id if result.matched_rule else None
            source = result.source.value if hasattr(result.source, "value") else str(result.source)
        except _VALIDATION_ERRORS as exc:
            raise HTTPException(status_code=422, detail=f"malformed policy response: {exc}") from exc
        if decision not in {"ALLOW", "DENY", "APPROVAL_REQUIRED"}:
            raise HTTPException(status_code=503, detail="policy evaluator returned an invalid decision")
        audit_reason = f"{reason} source={source}"
        self._audit_decision(
            tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
            session_id=session_id, trace_id=trace_id, request_id=request_id,
            action=act, resource=res, decision=decision, policy_version=pv,
            reason=audit_reason,
        )
        if source == "validation":
            raise HTTPException(status_code=422, detail=f"malformed policy request: {reason}")
        if decision == "DENY":
            raise HTTPException(status_code=403, detail=f"policy denied: {reason} (source={source})")
        if decision == "APPROVAL_REQUIRED":
            raise HTTPException(status_code=403, detail=f"approval required: {reason} (source={source})")
        from types import SimpleNamespace as _NS
        return _NS(allowed=True, decision=decision, reason=reason, source=source, matched_rule_id=pv)

_GATE_CACHE: dict[str, MattermostPolicyGate] = {}
def get_mattermost_gate(tenant_id: str = "default") -> MattermostPolicyGate:
    cached = _GATE_CACHE.get(tenant_id)
    if cached is not None and cached._engine is not None:
        cached._resolve_engine(tenant_id)
        return cached
    gate = MattermostPolicyGate(tenant_id)
    _GATE_CACHE[tenant_id] = gate
    return gate

def clear_mattermost_gate_cache() -> None:
    _GATE_CACHE.clear()
