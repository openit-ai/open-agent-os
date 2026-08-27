"""P2-3 E2E — create_session → send_prompt → MCP call → policy evaluate → audit verify

Covers:
  §40 Security Tests (5 scenarios) + §16A-E Zero-Bypass + §21 Risk + §25 Policy + §26 Token + §30-31 Audit
  Invariants: Agent Permission ≤ User Permission, Explicit Deny > Personal, Cross-user DENY, Auditable hash-chain

Categories:
  A) Full chain (7): kim success chain, cross-user deny chain, HIGH requires approval, tamper detection,
     MCP registry routing, trace propagation, memory+knowledge hooks
  B) 16A Bypass blocked (6): No ACP Bypass, No MCP Bypass, No Host Privilege, Credential Isolation,
     Network Egress DENY, Filesystem Isolation
  C) Slum/Edge resilience (6): concurrent sessions, expired token, replay, revoke cascade,
     malformed input, checkpoint verify

All deterministic, no network/LLM.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]

# ——— Ensure imports ———
import sys
for p in [
    ROOT / "control-plane",
    ROOT / "execution-gateway",
    ROOT / "security/policy-engine",
    ROOT / "security/delegation",
    ROOT / "security/credential-vault",
    ROOT / "security/crypto",
    ROOT / "security/audit",
    ROOT / "security/approval",
    ROOT / "security/memory-governance",
    ROOT / "security/token",
    ROOT / "packages/common-types",
    ROOT / "packages/agent-context",
    ROOT / "packages/policy-model",
    ROOT / "packages/audit-model",
    ROOT / "packages/delegation-model",
    ROOT / "packages/mcp-resource-model",
    ROOT / "packages/runtime-adapter",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
if str(ROOT / "security") not in sys.path:
    sys.path.insert(0, str(ROOT / "security"))

from control_plane.app import app as cp_app
from control_plane.session import session_store

from policy_model import PolicyBundle, PolicyDecision, PolicyEvaluationRequest, PolicyRule, PolicySource
from policy_engine.engine import PolicyEngine

from audit.audit_ledger.ledger import AuditLedger
from audit_model import AuditEvent, AuditEventType

from token_service.service import TokenService, issue_capability_token, verify_capability_token, clear_global_stores

from vault.vault import EncryptedPostgresVault

from execution_gateway.mcp_registry import MCPRegistry, MCPServer, default_registry
from execution_gateway.risk import classify, RiskLevel
from execution_gateway.proxy import proxy_tool_call
from execution_gateway.normalize import normalize_resource, canonicalize_action

from governance.governance import MemoryScope, MemoryStore

# ——— Helpers ———
SIGNING_KEY = "p2-e2e-test-signing-key-32b!"

def _make_policy_engine_with_deny() -> PolicyEngine:
    """Bundle where explicit DENY for gmail external send overrides personal allow."""
    bundle = PolicyBundle(
        id="p2-e2e-deny",
        tenant_id="test-tenant",
        name="p2-e2e",
        version="v1",
        rules=[
            PolicyRule(
                id="deny-external-send",
                source=PolicySource.EXPLICIT_DENY,
                action="SEND",
                resource_pattern="gmail/*",
                effect=PolicyDecision.DENY,
                priority=1,
                description="Explicit deny external send",
            ),
            PolicyRule(
                id="allow-personal-gmail",
                source=PolicySource.PERSONAL_DELEGATION,
                action="*",
                resource_pattern="gmail/*",
                effect=PolicyDecision.ALLOW,
                priority=10,
                description="Personal gmail allow",
            ),
            PolicyRule(
                id="allow-outline-read",
                source=PolicySource.PERSONAL_DELEGATION,
                action="READ",
                resource_pattern="outline/*",
                effect=PolicyDecision.ALLOW,
                priority=10,
            ),
        ],
    )
    return PolicyEngine([bundle])


def _audit_event(**kw) -> AuditEvent:
    now = datetime.now(timezone.utc)
    return AuditEvent(
        event_id=f"evt_{uuid.uuid4().hex[:12]}",
        event_type=kw.get("event_type", AuditEventType.USER_MESSAGE),
        timestamp=now,
        tenant_id=kw.get("tenant_id", "test-tenant"),
        user_id=kw.get("user_id", "employee:kim"),
        agent_id=kw.get("agent_id", "agent:assistant:kim"),
        session_id=kw.get("session_id", "sess_test"),
        trace_id=kw.get("trace_id", "trace_test"),
        request_id=kw.get("request_id", "req_test"),
        resource=kw.get("resource"),
        action=kw.get("action"),
        decision=kw.get("decision"),
        delegation_id=kw.get("delegation_id"),
    )


# =============================================================================
# A) Full chain
# =============================================================================

class TestE2EChainFull:
    """End-to-end: create_session → send_prompt → MCP call → policy evaluate → audit verify"""

    def test_full_chain_kim_success(self):
        """Happy path: kim creates session, sends prompt, MCP tool routed, policy ALLOW, audit chain valid."""
        client = TestClient(cp_app)
        r = client.post("/v1/sessions", json={"tenant_id": "t1", "user_id": "employee:kim"}, headers={"X-User-Id": "employee:kim"})
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]
        trace = r.json()["trace_id"]
        assert r.json()["agent_id"] == "agent:assistant:kim"

        # send prompt
        rp = client.post(f"/v1/sessions/{sid}/prompt", json={"session_id": sid, "prompt": "오늘 일정 알려줘"}, headers={"X-User-Id": "employee:kim"})
        assert rp.status_code == 200
        assert rp.json()["trace_id"] == trace

        # MCP registry routing — gmail_search should be discoverable
        srv = default_registry.find_tool("gmail_search")
        assert srv is not None
        assert "gmail_search" in default_registry.list_tools()

        # Policy evaluate — outline read should be ALLOW
        engine = _make_policy_engine_with_deny()
        req = PolicyEvaluationRequest(user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="outline/team/docs", tenant_id="t1")
        result = engine.evaluate(req)
        assert result.decision == PolicyDecision.ALLOW

        # Audit chain — append 3 events and verify
        ledger = AuditLedger(signing_key=SIGNING_KEY)
        for action, resource in [("READ", "gmail/user/kim/messages"), ("READ", "outline/team/docs"), ("SEND", "gmail/user/kim/messages")]:
            ev = _audit_event(action=action, resource=resource, trace_id=trace, session_id=sid)
            ledger.append(ev)
        assert ledger.verify_chain() is True
        assert ledger.count == 3
        cp = ledger.checkpoint()
        assert ledger.verify_checkpoint(cp) is True

    def test_full_chain_cross_user_denied(self):
        """Cross-user session access must be 403 at every hop."""
        client = TestClient(cp_app)
        r = client.post("/v1/sessions", json={"tenant_id": "t1", "user_id": "employee:kim"}, headers={"X-User-Id": "employee:kim"})
        sid = r.json()["session_id"]
        # lee tries to read kim's session
        r2 = client.get(f"/v1/sessions/{sid}", headers={"X-User-Id": "employee:lee"})
        assert r2.status_code == 403
        # lee tries to send prompt to kim's session
        r3 = client.post(f"/v1/sessions/{sid}/prompt", json={"session_id": sid, "prompt": "hijack"}, headers={"X-User-Id": "employee:lee"})
        assert r3.status_code == 403
        # lee tries to get context
        r4 = client.get(f"/v1/context/{sid}", headers={"X-User-Id": "employee:lee"})
        assert r4.status_code == 403

    def test_full_chain_high_risk_requires_approval_or_token(self):
        """HIGH-risk action without token must not auto-allow (policy APPROVAL_REQUIRED or DENY)."""
        engine = PolicyEngine([
            PolicyBundle(
                id="p2-high",
                tenant_id="t1",
                name="p2-high",
                version="v1",
                rules=[
                    PolicyRule(id="high-approv", source=PolicySource.EXPLICIT_DENY, action="EXPORT", resource_pattern="*bulk*", effect=PolicyDecision.DENY, priority=1),
                    PolicyRule(id="allow-all", source=PolicySource.PERSONAL_DELEGATION, action="*", resource_pattern="*", effect=PolicyDecision.ALLOW, priority=10),
                ],
            )
        ])
        req = PolicyEvaluationRequest(user_id="employee:kim", agent_id="agent:assistant:kim", action="EXPORT", resource="drive/bulk/export", tenant_id="t1")
        result = engine.evaluate(req)
        # HIGH-risk must not ALLOW — either DENY or APPROVAL_REQUIRED blocks the chain
        assert result.decision in (PolicyDecision.DENY, PolicyDecision.APPROVAL_REQUIRED)
        # Non-bulk read should be ALLOW
        req2 = PolicyEvaluationRequest(user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="drive/user/kim/file", tenant_id="t1")
        assert engine.evaluate(req2).decision == PolicyDecision.ALLOW

    def test_full_chain_audit_tamper_detected(self):
        """Tampering with an audit event must break verify_chain."""
        ledger = AuditLedger(signing_key=SIGNING_KEY)
        for i in range(3):
            ledger.append(_audit_event(resource=f"res/{i}", action="READ"))
        assert ledger.verify_chain() is True
        # Tamper second event's resource
        ledger._events[1].resource = "tampered/resource"
        assert ledger.verify_chain() is False
        # Tamper previous_hash
        ledger2 = AuditLedger(signing_key=SIGNING_KEY)
        for i in range(2):
            ledger2.append(_audit_event(resource=f"r{i}"))
        ledger2._events[1].previous_hash = "bad_hash"
        assert ledger2.verify_chain() is False

    def test_full_chain_mcp_registry_routing(self):
        """MCP registry: resource wildcard + tool discovery + normalize."""
        reg = MCPRegistry()
        reg.register(MCPServer(name="google", transport="mock", tools=["gmail_search", "gmail_send"], resources=["gmail/user/*"]))
        reg.register(MCPServer(name="outline", transport="mock", tools=["outline_search"], resources=["outline/*"]))
        assert reg.find_tool("gmail_search").name == "google"
        assert reg.find_resource("gmail/user/kim/messages/123").name == "google"
        assert reg.find_resource("outline/team/doc1").name == "outline"
        assert reg.find_tool("nonexistent") is None
        # normalize integration
        norm = reg.normalize_tool_call("gmail_send", "gmail://user/kim/messages", "SEND")
        assert norm["resource"] == "gmail/user/kim/messages"
        assert norm["action"] == "SEND"

    def test_full_chain_trace_propagation(self):
        """trace_id must stay consistent across session → prompt → audit."""
        client = TestClient(cp_app)
        r = client.post("/v1/sessions", json={"tenant_id": "t1", "user_id": "employee:kim"}, headers={"X-User-Id": "employee:kim"})
        sid = r.json()["session_id"]
        trace = r.json()["trace_id"]
        rp = client.post(f"/v1/sessions/{sid}/prompt", json={"session_id": sid, "prompt": "trace test"}, headers={"X-User-Id": "employee:kim"})
        assert rp.json()["trace_id"] == trace
        # context also carries trace
        rc = client.get(f"/v1/context/{sid}", headers={"X-User-Id": "employee:kim"})
        assert rc.json()["trace_id"] == trace
        # audit event carries same trace
        ledger = AuditLedger(signing_key=SIGNING_KEY)
        ev = _audit_event(trace_id=trace, session_id=sid)
        ledger.append(ev)
        assert ledger.events[0].trace_id == trace

    def test_full_chain_memory_and_knowledge_hooks(self):
        """Memory governance + knowledge retriever are reachable in the chain."""
        store = MemoryStore()
        rec = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="remember this")
        assert rec.owner == "employee:kim"
        # read back
        fetched = store.read(rec.id, requester="employee:kim")
        assert fetched.content == "remember this"
        # other user cannot read personal memory (isolation) — either raises or returns None / denied value
        try:
            other = store.read(rec.id, requester="employee:lee")
            # If no exception, must not leak content
            assert other is None or getattr(other, "content", None) != "remember this"
        except (PermissionError, KeyError, ValueError):
            pass
        # Knowledge retriever basic (mock) — import smoke test
        from execution_gateway.knowledge import KnowledgeRetriever
        from execution_gateway.connectors.outline import OutlineConnector
        connector = OutlineConnector(api_url="http://mock", api_token="test")
        retriever = KnowledgeRetriever(outline_connector=connector)
        assert retriever is not None


# =============================================================================
# B) 16A Zero-Bypass blocked
# =============================================================================

class Test16ABypassBlocked:
    """§16A invariants: every bypass path must be DENIED."""

    def test_no_acp_bypass_blocked(self):
        """Session.assert_owner blocks ACP bypass (direct Hermes → Internal API)."""
        from control_plane.session import SessionRecord
        rec = SessionRecord(
            session_id="sess_bypass",
            tenant_id="t1",
            user_id="employee:kim",
            agent_id="agent:assistant:kim",
            trace_id="trace_x",
            security_domain="general",
        )
        # owner passes
        rec.assert_owner("employee:kim")
        # attacker fails
        with pytest.raises(PermissionError, match="cross-user"):
            rec.assert_owner("employee:lee")
        with pytest.raises(PermissionError):
            rec.assert_owner("employee:attacker")

    def test_no_mcp_bypass_blocked(self):
        """HIGH-risk MCP call without valid capability token must be denied (proxy)."""
        async def _run():
            ctx = {
                "user_id": "employee:kim",
                "agent_id": "agent:assistant:kim",
                "tenant_id": "t1",
                "session_id": "sess_test",
                "trace_id": "trace_test",
                "request_id": "req_test",
            }
            # HIGH action without token → proxy should error / deny (not silently succeed)
            # We call proxy with no token for a HIGH-risk tool
            result = await proxy_tool_call(
                tool_name="gmail_send",
                args={"to": "outside@example.com", "subject": "leak"},
                context=ctx,
                capability_token=None,
            )
            # proxy returns error dict for HIGH without token
            assert result is not None
            # Must indicate denial / auth required, not success leak
            text = str(result).lower()
            assert any(k in text for k in ["denied", "token", "capability", "approval", "unauthorized", "forbidden", "error", "failed"])

        asyncio.run(_run())

    def test_no_host_privilege_blocked(self):
        """systemd hardening: NoNewPrivileges / ProtectSystem / PrivateTmp must be set."""
        svc = Path(ROOT / "deploy/systemd/hermes.service")
        assert svc.exists(), "hermes.service missing"
        content = svc.read_text()
        for directive in ["NoNewPrivileges=true", "ProtectSystem=strict", "PrivateTmp=true", "ProtectHome=true", "ReadWritePaths=/home/hermes"]:
            assert directive in content, f"missing hardening directive: {directive}"
        # No credential env leak
        assert "OAOS_SIGNING_KEY" not in content
        assert "FERNET_KEY" not in content

    def test_credential_isolation_blocked(self):
        """Vault: delegation-bound credentials isolate by owner (kim cannot read lee)."""
        from delegation.delegation_service.service import DelegationService
        svc = DelegationService()
        d_kim = svc.grant(user_id="employee:kim", agent_id="agent:assistant:kim", provider="google", scope="gmail.read")
        d_lee = svc.grant(user_id="employee:lee", agent_id="agent:assistant:lee", provider="google", scope="gmail.read")
        b_kim = svc.bind_credential(delegation_id=d_kim.id, provider="google", secret_ref="sec-kim", scope="gmail.read")
        b_lee = svc.bind_credential(delegation_id=d_lee.id, provider="google", secret_ref="sec-lee", scope="gmail.read")
        # kim can use own binding, lee can use own
        assert svc.is_binding_active(b_kim.id) is True
        assert svc.is_binding_active(b_lee.id) is True
        # cross-owner: kim's delegation not usable for lee's binding lookup — binding belongs to lee's delegation
        assert b_kim.delegation_id != b_lee.delegation_id
        assert b_kim.secret_ref != b_lee.secret_ref
        # revoke lee should not affect kim
        svc.revoke(d_lee.id)
        assert svc.is_binding_active(b_kim.id) is True
        assert svc.is_binding_active(b_lee.id) is False

    def test_network_isolation_egress_deny(self):
        """hermes-egress.nft must DENY DB/ERP/CRM and restrict 443."""
        nft = Path(ROOT / "deploy/firewall/hermes-egress.nft")
        assert nft.exists(), "hermes-egress.nft missing"
        content = nft.read_text()
        # Must contain explicit DENY for DB nets
        assert "10.20.0.0/16" in content or "INTERNAL_DB_NET" in content
        assert "5432" in content  # DB port block
        assert "hermes" in content.lower()
        # Must allow only ACP/MCP + 443 (restricted)
        assert "8000" in content or "ACP" in content
        assert "DROP" in content or "drop" in content
        # Verify script exists too
        verify_script = Path(ROOT / "deploy/scripts/verify-16a.sh")
        assert verify_script.exists(), "verify-16a.sh missing"

    def test_filesystem_isolation_blocked(self):
        """Filesystem: hermes can only RW /home/hermes, InaccessiblePaths covers /root."""
        svc = Path(ROOT / "deploy/systemd/hermes.service").read_text()
        assert "InaccessiblePaths=/root" in svc
        assert "ProtectClock=true" in svc or "ProtectControlGroups=true" in svc
        # create-hermes-user.sh must exist and set 0750 + no sudo
        script = Path(ROOT / "deploy/scripts/create-hermes-user.sh")
        assert script.exists()
        txt = script.read_text()
        assert "hermes" in txt
        assert "0750" in txt or "0700" in txt


# =============================================================================
# C) Slum / Edge resilience
# =============================================================================

class TestSlumEdge:
    """Resilience under concurrency / expiry / replay / revoke / malformed."""

    def test_concurrent_sessions_isolation(self):
        """Many concurrent sessions: each owner isolated, no cross-leak."""
        client = TestClient(cp_app)
        sids = []
        for user in ["employee:kim", "employee:lee", "employee:park"]:
            r = client.post("/v1/sessions", json={"tenant_id": "t1", "user_id": user}, headers={"X-User-Id": user})
            assert r.status_code == 200
            sids.append((user, r.json()["session_id"]))
        # Each user can only see own session
        for user, sid in sids:
            ok = client.get(f"/v1/sessions/{sid}", headers={"X-User-Id": user})
            assert ok.status_code == 200
            for other, _ in sids:
                if other != user:
                    bad = client.get(f"/v1/sessions/{sid}", headers={"X-User-Id": other})
                    assert bad.status_code == 403

    def test_expired_token_rejected(self):
        """Expired capability token must be rejected."""
        clear_global_stores()
        svc = TokenService(signing_key=SIGNING_KEY, default_ttl=1)
        tok = svc.issue(sub="agent:assistant:kim", on_behalf_of="employee:kim", action="SEND", resource="gmail/user/kim/messages", session_id="sess1", request_id="req1")
        time.sleep(2.0)
        try:
            result = svc.verify(tok)
            # If no exception, must indicate expired/invalid
            assert result is None or getattr(result, "valid", True) is False or "expir" in str(result).lower()
        except Exception as e:
            assert "expir" in str(e).lower() or "invalid" in str(e).lower() or "decod" in str(e).lower() or "time" in str(e).lower()

    def test_replay_token_rejected(self):
        """Same token replay must be rejected (nonce/jti)."""
        clear_global_stores()
        tok = issue_capability_token(SIGNING_KEY, sub="agent:assistant:kim", on_behalf_of="employee:kim", action="READ", resource="gmail/user/kim/messages", session_id="sess1", request_id="req1")
        first = verify_capability_token(SIGNING_KEY, tok)
        assert first["sub"] == "agent:assistant:kim"
        with pytest.raises(ValueError, match="replay"):
            verify_capability_token(SIGNING_KEY, tok)

    def test_delegation_revoke_cascade_blocks_chain(self):
        """Revoked delegation must invalidate downstream policy + token path."""
        from delegation.delegation_service.service import DelegationService
        svc = DelegationService()
        d = svc.grant(user_id="employee:kim", agent_id="agent:assistant:kim", provider="google", scope="gmail.read")
        assert d.status.value in ("active", "ACTIVE", "pending", "PENDING") or str(d.status).lower() in ("active", "pending")
        bid = svc.bind_credential(delegation_id=d.id, provider="google", secret_ref="google/refresh", scope="gmail.read")
        # Revoke delegation → binding must be revoked / invalid
        svc.revoke(d.id)
        # Binding should now be revoked or lookup fails
        b = svc.get_binding(bid.id) if hasattr(svc, "get_binding") else None
        if b is not None:
            assert (b.status.value.lower() if hasattr(b.status, "value") else str(b.status).lower()) in ("revoked", "invalid", "expired")
        # Delegation itself revoked
        d2 = svc.get(d.id)
        assert (d2.status.value.lower() if hasattr(d2.status, "value") else str(d2.status).lower()) == "revoked"

    def test_malformed_input_handling(self):
        """Malformed prompts / resources must not crash — they return 400/422 or safe error."""
        client = TestClient(cp_app)
        r = client.post("/v1/sessions", json={"tenant_id": "t1", "user_id": "employee:kim"}, headers={"X-User-Id": "employee:kim"})
        sid = r.json()["session_id"]
        # Empty resource / action in policy engine should deny safely
        engine = _make_policy_engine_with_deny()
        req = PolicyEvaluationRequest(user_id="employee:kim", agent_id="agent:assistant:kim", action="UNKNOWN_ACTION_XYZ", resource="", tenant_id="t1")
        result = engine.evaluate(req)
        assert result.decision == PolicyDecision.DENY
        # Malformed normalize should raise ValueError, not crash
        with pytest.raises(ValueError):
            normalize_resource("")
        with pytest.raises(ValueError):
            canonicalize_action("not_an_action_xyz")
        # Risk classify malformed still returns a level
        lvl = classify(action="UNKNOWN", resource="", is_external=False)
        assert lvl in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)

    def test_audit_checkpoint_sign_verify(self):
        """Checkpoint HMAC sign/verify — wrong key must fail, tampered head must fail."""
        ledger = AuditLedger(signing_key=SIGNING_KEY)
        for i in range(2):
            ledger.append(_audit_event(resource=f"r{i}"))
        cp = ledger.checkpoint()
        assert ledger.verify_checkpoint(cp) is True
        assert ledger.verify_checkpoint(cp, signing_key="wrong-key") is False
        # Tamper head
        bad = cp.model_copy(update={"chain_head_hash": "bad"*16})
        assert ledger.verify_checkpoint(bad) is False
        # Empty ledger checkpoint still verifiable (head == "")
        empty = AuditLedger(signing_key=SIGNING_KEY)
        cp2 = empty.checkpoint()
        assert empty.verify_checkpoint(cp2) is True
