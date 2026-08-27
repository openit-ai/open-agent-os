"""Open Agent OS — Security & Governance FastAPI (Section 7.3).
Endpoints:
  POST /v1/policy/evaluate
  POST /v1/delegation/grant
  POST /v1/token/issue
  POST /v1/approval/request
  POST /v1/audit/verify
"""
from __future__ import annotations

import sys
import os

# security 하위 패키지 경로를 sys.path에 추가 (editable install 없이 동작)
# - policy_engine, vault, crypto 는 2단계 패키지이므로 전용 경로 필요
# - delegation/approval/audit/token 은 security 루트 자체로 resolve
_sys_root = os.path.dirname(__file__)
sys.path.insert(0, _sys_root)  # delegation, approval, audit
sys.path.insert(0, os.path.join(_sys_root, "policy-engine"))
sys.path.insert(0, os.path.join(_sys_root, "credential-vault"))
sys.path.insert(0, os.path.join(_sys_root, "token"))
sys.path.insert(0, os.path.join(_sys_root, "crypto"))

from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# 모델 import
from policy_model import PolicyBundle, PolicyDecision, PolicyEvaluationRequest, PolicyEvaluationResult
from policy_engine.engine import PolicyEngine
from policy_engine.default_bundle import default_bundle
from delegation_model import Delegation
from delegation.delegation_service.service import DelegationService
from token_service.service import TokenService, clear_global_stores
from approval.approval_workflow.workflow import ApprovalStore, ApprovalDecision
from audit.audit_ledger.ledger import AuditLedger
import hashlib
import uuid

from audit_model import AuditEvent, AuditEventType

app = FastAPI(title="Open Agent OS — Security & Governance", version="0.1.1")

# ── 전역 싱글톤 (프로세스 내 공유) ──────────────────────────────
SIGNING_KEY = os.environ.get("OAOS_SIGNING_KEY", "dev-signing-key-please-change")
ENCRYPTION_KEY = os.environ.get("OAOS_ENCRYPTION_KEY", "dev-encryption-key-32bytes!!").encode()

delegation_service = DelegationService()
token_service = TokenService(signing_key=SIGNING_KEY)
approval_store = ApprovalStore(signing_key=SIGNING_KEY)
audit_ledger = AuditLedger(signing_key=SIGNING_KEY)
policy_engine = PolicyEngine(bundles=[default_bundle(tenant_id="default")])


# ── Request / Response 모델 ────────────────────────────────────
class DelegationGrantRequest(BaseModel):
    user_id: str
    agent_id: str
    provider: str
    scope: str


class DelegationRevokeRequest(BaseModel):
    delegation_id: str


class TokenIssueRequest(BaseModel):
    sub: str
    on_behalf_of: str
    action: str
    resource: str
    session_id: str
    request_id: str
    delegation_id: str | None = None
    ttl_seconds: int = 300


class TokenVerifyRequest(BaseModel):
    token: str


class ApprovalRequestBody(BaseModel):
    user_id: str
    agent_id: str
    action: str
    resource: str
    risk: str = "HIGH"
    ttl_minutes: int = 60


class ApprovalDecideBody(BaseModel):
    approval_id: str
    decision: ApprovalDecision
    decided_by: str
    group_id: str | None = None


class AuditVerifyRequest(BaseModel):
    chain_head_hash: str | None = None
    signature: str | None = None


# ── Health ─────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}


# ── Policy evaluate ────────────────────────────────────────────
@app.post("/v1/policy/evaluate", response_model=PolicyEvaluationResult)
def policy_evaluate(req: PolicyEvaluationRequest):
    """Section 25 — deterministic policy evaluation."""
    result = policy_engine.evaluate(req)
    # audit 기록
    try:
        evt = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type=AuditEventType.POLICY_DECISION,
            timestamp=datetime.now(timezone.utc),
            tenant_id=req.tenant_id,
            user_id=req.user_id,
            agent_id=req.agent_id,
            resource=req.resource,
            action=req.action,
            decision=result.decision.value,
            policy_version=result.policy_version,
        )
        audit_ledger.append(evt)
    except Exception:
        pass
    return result


# ── Delegation ─────────────────────────────────────────────────
@app.post("/v1/delegation/grant", response_model=Delegation)
def delegation_grant(req: DelegationGrantRequest):
    d = delegation_service.grant(
        user_id=req.user_id, agent_id=req.agent_id, provider=req.provider, scope=req.scope
    )
    try:
        evt = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type=AuditEventType.DELEGATION_CREATED,
            timestamp=datetime.now(timezone.utc),
            tenant_id="default",
            user_id=req.user_id,
            agent_id=req.agent_id,
            delegation_id=d.id,
        )
        audit_ledger.append(evt)
    except Exception:
        pass
    return d


@app.post("/v1/delegation/revoke")
def delegation_revoke(req: DelegationRevokeRequest):
    d = delegation_service.revoke(req.delegation_id)
    if d is None:
        raise HTTPException(status_code=404, detail="delegation not found")
    try:
        evt = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type=AuditEventType.DELEGATION_REVOKED,
            timestamp=datetime.now(timezone.utc),
            tenant_id="default",
            user_id=d.user_id,
            agent_id=d.agent_id,
            delegation_id=d.id,
        )
        audit_ledger.append(evt)
    except Exception:
        pass
    return {"status": "revoked", "delegation_id": d.id, "delegation": d}


@app.get("/v1/delegation/{delegation_id}")
def delegation_get(delegation_id: str):
    d = delegation_service.get(delegation_id)
    if d is None:
        raise HTTPException(status_code=404, detail="not found")
    return d


# ── Token ──────────────────────────────────────────────────────
@app.post("/v1/token/issue")
def token_issue(req: TokenIssueRequest):
    # revoke 된 delegation 으로는 발급 불가
    if req.delegation_id and not delegation_service.is_active(req.delegation_id):
        raise HTTPException(status_code=403, detail="delegation not active or revoked")
    token = token_service.issue(
        sub=req.sub,
        on_behalf_of=req.on_behalf_of,
        action=req.action,
        resource=req.resource,
        session_id=req.session_id,
        request_id=req.request_id,
        delegation_id=req.delegation_id,
        ttl_seconds=req.ttl_seconds,
    )
    try:
        evt = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type=AuditEventType.CAPABILITY_ISSUED,
            timestamp=datetime.now(timezone.utc),
            tenant_id="default",
            user_id=req.on_behalf_of,
            agent_id=req.sub,
            resource=req.resource,
            action=req.action,
            delegation_id=req.delegation_id,
        )
        audit_ledger.append(evt)
    except Exception:
        pass
    return {"token": token}


@app.post("/v1/token/verify")
def token_verify(req: TokenVerifyRequest):
    try:
        payload = token_service.verify(req.token)
        return {"valid": True, "payload": payload}
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/v1/token/revoke")
def token_revoke(req: TokenVerifyRequest):
    token_service.revoke(req.token)
    return {"status": "revoked"}


# ── Approval ───────────────────────────────────────────────────
@app.post("/v1/approval/request")
def approval_request(req: ApprovalRequestBody):
    ar = approval_store.create(
        user_id=req.user_id,
        agent_id=req.agent_id,
        action=req.action,
        resource=req.resource,
        risk=req.risk,
        ttl_minutes=req.ttl_minutes,
    )
    try:
        evt = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type=AuditEventType.APPROVAL_REQUEST,
            timestamp=datetime.now(timezone.utc),
            tenant_id="default",
            user_id=req.user_id,
            agent_id=req.agent_id,
            resource=req.resource,
            action=req.action,
        )
        audit_ledger.append(evt)
    except Exception:
        pass
    return ar


@app.post("/v1/approval/decide")
def approval_decide(req: ApprovalDecideBody):
    try:
        ar = approval_store.decide(
            approval_id=req.approval_id,
            decision=req.decision,
            decided_by=req.decided_by,
            group_id=req.group_id,
        )
        try:
            evt = AuditEvent(
                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                event_type=AuditEventType.APPROVAL_DECISION,
                timestamp=datetime.now(timezone.utc),
                tenant_id="default",
                user_id=ar.user_id,
                agent_id=ar.agent_id,
                resource=ar.resource,
                action=ar.action,
                decision=req.decision.value,
            )
            audit_ledger.append(evt)
        except Exception:
            pass
        return ar
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/v1/approval/{approval_id}")
def approval_get(approval_id: str):
    ar = approval_store.get(approval_id)
    if ar is None:
        raise HTTPException(status_code=404, detail="not found")
    return ar


# ── Audit ──────────────────────────────────────────────────────
@app.post("/v1/audit/verify")
def audit_verify(req: AuditVerifyRequest | None = None):
    """Hash-chain 검증 + optional checkpoint 검증."""
    chain_valid = audit_ledger.verify_chain()
    result: dict = {
        "chain_valid": chain_valid,
        "event_count": audit_ledger.count,
        "head": audit_ledger.head,
    }
    # checkpoint 검증이 요청된 경우
    if req and req.chain_head_hash and req.signature:
        from audit_model import AuditCheckpoint

        cp = AuditCheckpoint(
            chain_head_hash=req.chain_head_hash,
            event_count=audit_ledger.count,
            created_at=datetime.now(timezone.utc),
            signature=req.signature,
        )
        result["checkpoint_valid"] = audit_ledger.verify_checkpoint(cp)
    return result


@app.get("/v1/audit/checkpoint")
def audit_checkpoint():
    cp = audit_ledger.checkpoint()
    return cp


@app.get("/v1/audit/events")
def audit_events():
    return {"events": [e.model_dump(mode="json") for e in audit_ledger.events]}
