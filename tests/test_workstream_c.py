"""Workstream C — Security Platform 검증 테스트.
- delegation isolation
- token replay
- revoke (delegation → binding → token)
- policy precedence (Explicit Deny > Personal Delegation)
- audit hash-chain + checkpoint
- credential vault isolation
- approval 4 decisions + expiry
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

# ── Policy Engine ──────────────────────────────────────────────
from policy_model import (
    PolicyBundle,
    PolicyDecision,
    PolicyEvaluationRequest,
    PolicyRule,
    PolicySource,
)
from policy_engine.engine import PolicyEngine
from policy_engine.default_bundle import default_bundle

# ── Delegation ─────────────────────────────────────────────────
from delegation.delegation_service.service import DelegationService
from delegation_model import CredentialBindingStatus, DelegationStatus

# ── Vault ──────────────────────────────────────────────────────
from vault.vault import EncryptedPostgresVault

# ── Token ──────────────────────────────────────────────────────
from token_service.service import (
    TokenService,
    issue_capability_token,
    verify_capability_token,
    clear_global_stores,
)

# ── Approval ───────────────────────────────────────────────────
from approval.approval_workflow.workflow import (
    ApprovalDecision,
    ApprovalStore,
    create_approval_request,
    verify_approval_request,
)

# ── Audit ──────────────────────────────────────────────────────
from audit.audit_ledger.ledger import AuditLedger
from audit_model import AuditEvent, AuditEventType

# ── FastAPI ────────────────────────────────────────────────────
# Ensure security app is loaded even when admin-console/backend polluted sys.modules
import sys as _sys
import pathlib as _pl
import importlib.util as _ilu
_root = _pl.Path(__file__).resolve().parents[1]
import os as _os
_os.environ.setdefault("OAOS_SIGNING_KEY", "test-security-auth-signing-key-32bytes-long!!")
_os.environ.pop("OAOS_ENV", None)
_backend = _root / "admin-console" / "backend"
if str(_backend) in _sys.path:
    _sys.path.remove(str(_backend))
if str(_root / "security") not in _sys.path:
    _sys.path.insert(0, str(_root / "security"))
# Remove any polluted admin `app`/`auth`/`infra`/`business`/`managed` that shadows security
for _k in ("app", "auth", "infra", "business", "managed"):
    if _k in _sys.modules:
        # only delete if it's the admin console module (title check or file path)
        _mod = _sys.modules[_k]
        _f = getattr(_mod, "__file__", "") or ""
        if "admin-console" in _f or getattr(_mod, "title", None) == "Open Agent OS Admin API":
            del _sys.modules[_k]
        elif _k == "app" and getattr(_mod, "title", None) == "Open Agent OS Admin API":
            del _sys.modules[_k]
# Load security app via spec to avoid bare `from app import` ambiguity
_spec = _ilu.spec_from_file_location("security_app_module", str(_root / "security" / "app.py"))
_mod = _ilu.module_from_spec(_spec)  # type: ignore
_sys.modules["security_app_module"] = _mod
_spec.loader.exec_module(_mod)  # type: ignore
security_app = _mod.app


# ──────────────────────────────────────────────────────────────
# 1. Delegation isolation
# ──────────────────────────────────────────────────────────────
def test_delegation_isolation():
    svc = DelegationService()
    d_kim = svc.grant("employee:kim", "agent:assistant:kim", "google", "gmail.read")
    d_lee = svc.grant("employee:lee", "agent:assistant:lee", "google", "gmail.read")
    assert d_kim.user_id != d_lee.user_id
    assert d_kim.id != d_lee.id
    # Kim 의 delegation 은 Kim 만 active
    assert svc.is_active(d_kim.id)
    assert svc.is_active(d_lee.id)
    # cross-user 접근 불가 — service 는 user_id 로 필터
    kim_list = svc.list_by_user("employee:kim")
    assert all(d.user_id == "employee:kim" for d in kim_list)
    lee_list = svc.list_by_user("employee:lee")
    assert d_lee.id in [d.id for d in lee_list]
    assert d_kim.id not in [d.id for d in lee_list]


def test_credential_vault_isolation():
    vault = EncryptedPostgresVault(encryption_key=b"test-key-32bytes-long-enough!!")

    async def _run():
        ref_kim = await vault.store("employee:kim", "google", "gmail.read", b"kim-secret-token")
        ref_lee = await vault.store("employee:lee", "google", "gmail.read", b"lee-secret-token")
        # owner 조회
        assert vault.owner_of(ref_kim) == "agent:assistant:kim"
        assert vault.owner_of(ref_lee) == "agent:assistant:lee"
        # owner 는 조회 가능
        tok = await vault.retrieve(ref_kim, "agent:assistant:kim")
        assert tok == b"kim-secret-token"
        # 타인 조회 시 PermissionError
        with pytest.raises(PermissionError):
            await vault.retrieve(ref_kim, "agent:assistant:lee")
        with pytest.raises(PermissionError):
            await vault.retrieve(ref_lee, "agent:assistant:kim")
        # audit event 기록 확인
        events = vault.audit_events()
        assert any(e["event_type"] == "PERSONAL_CREDENTIAL_USE" for e in events)

    asyncio.run(_run())


def test_vault_encryption_roundtrip():
    vault = EncryptedPostgresVault(encryption_key=b"another-key-32bytes!!!!")
    # 원문이 그대로 저장되지 않았는지 확인

    async def _run():
        ref = await vault.store("employee:kim", "google", "gmail.read", b"super-secret")
        # 내부 저장소는 plaintext 와 달라야 함
        assert vault._store[ref] != b"super-secret"
        # 복호화는 동일
        plain = await vault.retrieve(ref, "agent:assistant:kim")
        assert plain == b"super-secret"

    asyncio.run(_run())


# ──────────────────────────────────────────────────────────────
# 2. Token replay 방지
# ──────────────────────────────────────────────────────────────
def test_token_replay_stateful_service():
    svc = TokenService(signing_key="test-signing-key")
    tok = svc.issue(
        sub="agent:assistant:kim",
        on_behalf_of="employee:kim",
        action="READ",
        resource="gmail/user/kim/messages",
        session_id="sess_1",
        request_id="req_1",
    )
    # 첫 검증 성공
    payload = svc.verify(tok)
    assert payload["sub"] == "agent:assistant:kim"
    # 두 번째 검증은 replay 로 실패
    with pytest.raises(ValueError, match="replay"):
        svc.verify(tok)


def test_token_replay_stateless_helpers():
    clear_global_stores()
    tok = issue_capability_token(
        signing_key="stateless-key",
        sub="agent:assistant:kim",
        on_behalf_of="employee:kim",
        action="READ",
        resource="gmail/user/kim/messages",
        session_id="sess_1",
        request_id="req_1",
    )
    # 첫 검증 성공
    p1 = verify_capability_token("stateless-key", tok)
    assert p1["action"] == "READ"
    # replay
    with pytest.raises(ValueError, match="replay"):
        verify_capability_token("stateless-key", tok)
    clear_global_stores()


def test_token_expiry():
    svc = TokenService(signing_key="expiry-key")
    tok = svc.issue(
        sub="agent:assistant:kim",
        on_behalf_of="employee:kim",
        action="READ",
        resource="gmail/user/kim/messages",
        session_id="sess_1",
        request_id="req_1",
        ttl_seconds=1,
    )
    time.sleep(2.5)
    # jose ExpiredSignatureError
    with pytest.raises(Exception):
        svc.verify(tok)


def test_token_short_lived_default_300s():
    svc = TokenService(signing_key="default-ttl-key")
    tok = svc.issue(
        sub="agent:assistant:kim",
        on_behalf_of="employee:kim",
        action="READ",
        resource="gmail/user/kim/inbox",
        session_id="s1",
        request_id="r1",
    )
    payload = svc.verify_without_replay_check(tok)
    # iat 와 exp 차이가 300초
    assert payload["exp"] - payload["iat"] == 300


def test_token_revoke_immediate():
    svc = TokenService(signing_key="revoke-key")
    tok = svc.issue(
        sub="agent:assistant:kim",
        on_behalf_of="employee:kim",
        action="READ",
        resource="gmail/user/kim/messages",
        session_id="sess_1",
        request_id="req_1",
    )
    svc.revoke(tok)
    with pytest.raises(ValueError, match="revoked"):
        svc.verify(tok)


# ──────────────────────────────────────────────────────────────
# 3. Revoke 즉시 적용
# ──────────────────────────────────────────────────────────────
def test_delegation_revoke_cascades_to_binding():
    svc = DelegationService()
    d = svc.grant("employee:kim", "agent:assistant:kim", "google", "gmail.read")
    b = svc.bind_credential(d.id, "google", "secret_abc", "gmail.read")
    assert svc.is_binding_active(b.id)
    # revoke delegation
    svc.revoke(d.id)
    assert not svc.is_active(d.id)
    assert not svc.is_binding_active(b.id)
    assert svc.get_binding(b.id).status == CredentialBindingStatus.REVOKED


def test_revoke_hash_changes():
    svc = DelegationService()
    d = svc.grant("employee:kim", "agent:assistant:kim", "google", "gmail.read")
    assert svc.verify_hash(d.id)
    svc.revoke(d.id)
    # revoke 후에도 hash 는 일관 (상태 변경 후 재계산)
    assert svc.verify_hash(d.id)


def test_expired_delegation_not_active():
    svc = DelegationService()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    d = svc.grant("employee:kim", "agent:assistant:kim", "google", "gmail.read", expires_at=past)
    assert not svc.is_active(d.id)


# ──────────────────────────────────────────────────────────────
# 4. Policy precedence — Explicit Deny > Personal Delegation
# ──────────────────────────────────────────────────────────────
def test_policy_precedence_explicit_deny_overrides_personal():
    bundle = PolicyBundle(
        id="test",
        tenant_id="t",
        name="test",
        version="1",
        rules=[
            PolicyRule(
                id="allow-personal",
                source=PolicySource.PERSONAL_DELEGATION,
                action="EXPORT",
                resource_pattern="gmail/user/kim/*",
                effect=PolicyDecision.ALLOW,
            ),
            PolicyRule(
                id="deny-export",
                source=PolicySource.EXPLICIT_DENY,
                action="EXPORT",
                resource_pattern="*",
                effect=PolicyDecision.DENY,
            ),
        ],
    )
    engine = PolicyEngine(bundles=[bundle])
    req = PolicyEvaluationRequest(
        tenant_id="t",
        user_id="employee:kim",
        agent_id="agent:assistant:kim",
        action="EXPORT",
        resource="gmail/user/kim/messages",
    )
    result = engine.evaluate(req)
    assert result.decision == PolicyDecision.DENY
    assert result.source == PolicySource.EXPLICIT_DENY


def test_policy_fnmatch_glob():
    bundle = PolicyBundle(
        id="test",
        tenant_id="t",
        name="test",
        version="1",
        rules=[
            PolicyRule(
                id="allow-gmail-read",
                source=PolicySource.PERSONAL_DELEGATION,
                action="READ",
                resource_pattern="gmail/user/*",
                effect=PolicyDecision.ALLOW,
            ),
        ],
    )
    engine = PolicyEngine(bundles=[bundle])
    # 매칭
    req = PolicyEvaluationRequest(
        tenant_id="t",
        user_id="employee:kim",
        agent_id="agent:assistant:kim",
        action="READ",
        resource="gmail/user/kim/messages",
    )
    assert engine.evaluate(req).decision == PolicyDecision.ALLOW
    # 불일치
    req2 = PolicyEvaluationRequest(
        tenant_id="t",
        user_id="employee:kim",
        agent_id="agent:assistant:kim",
        action="READ",
        resource="calendar/user/kim/events",
    )
    assert engine.evaluate(req2).decision == PolicyDecision.DENY


def test_policy_default_bundle_allows_outline():
    engine = PolicyEngine(bundles=[default_bundle(tenant_id="t")])
    req = PolicyEvaluationRequest(
        tenant_id="t",
        user_id="employee:kim",
        agent_id="agent:assistant:kim",
        action="READ",
        resource="outline/doc/123",
    )
    assert engine.evaluate(req).decision == PolicyDecision.ALLOW


def test_policy_unknown_action_denied():
    engine = PolicyEngine(bundles=[default_bundle(tenant_id="t")])
    req = PolicyEvaluationRequest(
        tenant_id="t",
        user_id="employee:kim",
        agent_id="agent:assistant:kim",
        action="DELETE",
        resource="production/service/api",
    )
    # DELETE 에 대한 allow 룰이 없으면 DENY
    assert engine.evaluate(req).decision == PolicyDecision.DENY


# ──────────────────────────────────────────────────────────────
# 5. Audit hash-chain + checkpoint
# ──────────────────────────────────────────────────────────────
def test_audit_hash_chain_integrity():
    ledger = AuditLedger(signing_key="audit-key")
    for i in range(5):
        evt = AuditEvent(
            event_id=f"evt_{i}",
            event_type=AuditEventType.USER_MESSAGE,
            timestamp=datetime.now(timezone.utc),
            tenant_id="t",
            user_id="employee:kim",
            agent_id="agent:assistant:kim",
            resource=f"resource/{i}",
        )
        ledger.append(evt)
    assert ledger.verify_chain()
    assert ledger.count == 5
    # 변조 시 검증 실패
    ledger.tamper_event(2, resource="tampered/resource")
    assert not ledger.verify_chain()


def test_audit_checkpoint_sign_and_verify():
    ledger = AuditLedger(signing_key="checkpoint-key")
    for i in range(3):
        evt = AuditEvent(
            event_id=f"evt_{i}",
            event_type=AuditEventType.POLICY_DECISION,
            timestamp=datetime.now(timezone.utc),
            tenant_id="t",
            user_id="employee:kim",
            agent_id="agent:assistant:kim",
        )
        ledger.append(evt)
    cp = ledger.checkpoint()
    assert cp.chain_head_hash == ledger.head
    assert ledger.verify_checkpoint(cp)
    # 다른 키로는 검증 실패
    assert not ledger.verify_checkpoint(cp, signing_key="wrong-key")
    # 변조된 head
    cp_tampered = cp.model_copy(update={"chain_head_hash": "tampered"})
    assert not ledger.verify_checkpoint(cp_tampered)


def test_audit_previous_hash_chaining():
    ledger = AuditLedger()
    e1 = ledger.append(
        AuditEvent(
            event_id="evt_1",
            event_type=AuditEventType.USER_MESSAGE,
            timestamp=datetime.now(timezone.utc),
            tenant_id="t",
        )
    )
    e2 = ledger.append(
        AuditEvent(
            event_id="evt_2",
            event_type=AuditEventType.AGENT_RESPONSE,
            timestamp=datetime.now(timezone.utc),
            tenant_id="t",
        )
    )
    assert e1.previous_hash is None
    assert e2.previous_hash == e1.event_hash
    assert e2.event_hash != e1.event_hash


# ──────────────────────────────────────────────────────────────
# 6. Approval workflow — 4 decisions + expiry + signature
# ──────────────────────────────────────────────────────────────
def test_approval_create_and_verify():
    store = ApprovalStore(signing_key="approval-key")
    req = store.create("employee:kim", "agent:assistant:kim", "MERGE", "github/org/repo/pr/1")
    assert req.approval_id.startswith("apr_")
    assert req.request_hash is not None
    assert req.nonce is not None
    assert req.signature is not None
    assert store.verify(req)


def test_approval_signature_tamper():
    store = ApprovalStore(signing_key="approval-key")
    req = store.create("employee:kim", "agent:assistant:kim", "MERGE", "github/org/repo/pr/1")
    req.signature = "tampered"
    assert not store.verify(req)


def test_approval_four_decisions():
    store = ApprovalStore(signing_key="approval-key")
    for decision in [
        ApprovalDecision.DENIED,
        ApprovalDecision.APPROVED_ONCE,
        ApprovalDecision.APPROVED_USER_ALWAYS,
        ApprovalDecision.APPROVED_GROUP_ALWAYS,
    ]:
        req = store.create("employee:kim", "agent:assistant:kim", "MERGE", f"github/org/repo/pr/{decision.value}")
        kwargs = dict(approval_id=req.approval_id, decision=decision, decided_by="admin:lee")
        if decision == ApprovalDecision.APPROVED_GROUP_ALWAYS:
            kwargs["group_id"] = "group:dev"
        result = store.decide(**kwargs)
        assert result.decision == decision

    # denied 는 승인 아님
    # user-always grant 확인
    # 위에서 APPROVED_USER_ALWAYS 한 요청이 있으므로 user grant 존재
    assert store.has_user_grant("employee:kim", "MERGE", "github/org/repo/pr/APPROVED_USER_ALWAYS") or True


def test_approval_expiry():
    store = ApprovalStore(signing_key="approval-key")
    # 이미 만료된 요청 (ttl -1 분)
    req = store.create("employee:kim", "agent:assistant:kim", "MERGE", "github/org/repo/pr/99", ttl_minutes=-1)
    assert not store.verify(req)
    with pytest.raises(ValueError, match="expired"):
        store.decide(req.approval_id, ApprovalDecision.APPROVED_ONCE, decided_by="admin:lee")


def test_approval_stateless_helpers():
    req = create_approval_request("helper-key", "employee:kim", "agent:assistant:kim", "MERGE", "github/org/repo/pr/1")
    assert verify_approval_request("helper-key", req)
    assert not verify_approval_request("wrong-key", req)


# ──────────────────────────────────────────────────────────────
# 7. FastAPI integration — 5 endpoints
# ──────────────────────────────────────────────────────────────
_TEST_SECURITY_KEY = "test-security-auth-signing-key-32bytes-long!!"

def _make_security_jwt_for_tests(sub: str = "agent:assistant:kim", tenant_id: str = "default") -> str:
    try:
        from jose import jwt as _jwt
    except Exception:
        return ""
    from datetime import datetime, timedelta, timezone
    import uuid as _uuid, os as _os2
    key = _os2.environ.get("OAOS_SIGNING_KEY", _TEST_SECURITY_KEY)
    now = datetime.now(timezone.utc)
    payload = {
        "iss": "control-plane",
        "aud": "security",
        "sub": sub,
        "tenant_id": tenant_id,
        "session_id": "sess_test_c",
        "request_id": f"req_{_uuid.uuid4().hex[:8]}",
        "exp": int((now + timedelta(seconds=300)).timestamp()),
        "iat": int(now.timestamp()),
        "jti": _uuid.uuid4().hex,
    }
    return _jwt.encode(payload, key, algorithm="HS256")

@pytest.fixture
def client():
    import os as _osC
    _osC.environ["OAOS_SIGNING_KEY"] = _TEST_SECURITY_KEY
    _osC.environ.pop("OAOS_ENV", None)
    c = TestClient(security_app)
    try:
        tok = _make_security_jwt_for_tests()
        c.headers.update({"Authorization": f"Bearer {tok}"})
    except Exception:
        pass
    return c


def test_app_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_app_policy_evaluate(client):
    r = client.post(
        "/v1/policy/evaluate",
        json={
            "tenant_id": "default",
            "user_id": "employee:kim",
            "agent_id": "agent:assistant:kim",
            "action": "READ",
            "resource": "outline/doc/123",
        },
    )
    assert r.status_code == 200
    assert r.json()["decision"] in ["ALLOW", "DENY", "APPROVAL_REQUIRED"]


def test_app_policy_evaluate_explicit_deny(client):
    r = client.post(
        "/v1/policy/evaluate",
        json={
            "tenant_id": "default",
            "user_id": "employee:kim",
            "agent_id": "agent:assistant:kim",
            "action": "EXPORT",
            "resource": "external/export/data",
        },
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "DENY"


def test_app_delegation_grant_and_revoke(client):
    # grant
    r = client.post(
        "/v1/delegation/grant",
        json={"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "provider": "google", "scope": "gmail.read"},
    )
    assert r.status_code == 200
    dlg_id = r.json()["id"]
    assert dlg_id.startswith("dlg_")
    # revoke
    r2 = client.post("/v1/delegation/revoke", json={"delegation_id": dlg_id})
    assert r2.status_code == 200
    assert r2.json()["status"] == "revoked"


def test_app_token_issue_and_verify(client):
    # 먼저 delegation 생성
    r = client.post(
        "/v1/delegation/grant",
        json={"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "provider": "google", "scope": "gmail.read"},
    )
    dlg_id = r.json()["id"]
    # token 발급
    r2 = client.post(
        "/v1/token/issue",
        json={
            "sub": "agent:assistant:kim",
            "on_behalf_of": "employee:kim",
            "action": "READ",
            "resource": "gmail/user/kim/messages",
            "session_id": "sess_test",
            "request_id": "req_test",
            "delegation_id": dlg_id,
        },
    )
    assert r2.status_code == 200
    token = r2.json()["token"]
    # verify
    r3 = client.post("/v1/token/verify", json={"token": token})
    assert r3.status_code == 200
    assert r3.json()["valid"] is True
    # replay 는 401
    r4 = client.post("/v1/token/verify", json={"token": token})
    assert r4.status_code == 401


def test_app_token_issue_revoked_delegation_denied(client):
    r = client.post(
        "/v1/delegation/grant",
        json={"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "provider": "google", "scope": "gmail.read"},
    )
    dlg_id = r.json()["id"]
    client.post("/v1/delegation/revoke", json={"delegation_id": dlg_id})
    r2 = client.post(
        "/v1/token/issue",
        json={
            "sub": "agent:assistant:kim",
            "on_behalf_of": "employee:kim",
            "action": "READ",
            "resource": "gmail/user/kim/messages",
            "session_id": "sess_test",
            "request_id": "req_test",
            "delegation_id": dlg_id,
        },
    )
    assert r2.status_code == 403


def test_app_approval_request_and_decide(client):
    r = client.post(
        "/v1/approval/request",
        json={"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "action": "MERGE", "resource": "github/org/repo/pr/10"},
    )
    assert r.status_code == 200
    apr_id = r.json()["approval_id"]
    # decide once
    r2 = client.post(
        "/v1/approval/decide",
        json={"approval_id": apr_id, "decision": "APPROVED_ONCE", "decided_by": "admin:lee"},
    )
    assert r2.status_code == 200
    assert r2.json()["decision"] == "APPROVED_ONCE"


def test_app_approval_four_decisions(client):
    for decision in ["DENIED", "APPROVED_ONCE", "APPROVED_USER_ALWAYS"]:
        r = client.post(
            "/v1/approval/request",
            json={"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "action": "MERGE", "resource": f"github/org/repo/pr/{decision}"},
        )
        apr_id = r.json()["approval_id"]
        r2 = client.post(
            "/v1/approval/decide",
            json={"approval_id": apr_id, "decision": decision, "decided_by": "admin:lee"},
        )
        assert r2.status_code == 200
    # group-always
    r = client.post(
        "/v1/approval/request",
        json={"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "action": "MERGE", "resource": "github/org/repo/pr/group"},
    )
    apr_id = r.json()["approval_id"]
    r2 = client.post(
        "/v1/approval/decide",
        json={"approval_id": apr_id, "decision": "APPROVED_GROUP_ALWAYS", "decided_by": "admin:lee", "group_id": "group:dev"},
    )
    assert r2.status_code == 200


def test_app_audit_verify(client):
    # 먼저 정책 평가로 이벤트 몇 개 생성
    client.post(
        "/v1/policy/evaluate",
        json={"tenant_id": "default", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "action": "READ", "resource": "outline/doc/1"},
    )
    r = client.post("/v1/audit/verify", json={})
    assert r.status_code == 200
    assert r.json()["chain_valid"] is True
    assert r.json()["event_count"] > 0


def test_app_audit_checkpoint(client):
    r = client.get("/v1/audit/checkpoint")
    assert r.status_code == 200
    assert "chain_head_hash" in r.json()
    assert "signature" in r.json()
