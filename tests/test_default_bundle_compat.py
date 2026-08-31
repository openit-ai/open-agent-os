"""Regression: default_bundle compatibility id and DELETE unknown DEFAULT_DENY."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for p in [ROOT / "security" / "policy-engine", ROOT / "packages" / "policy-model"]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from policy_engine.default_bundle import default_bundle
from policy_engine.small_business_bundle import small_business_bundle
from policy_engine.engine import PolicyEngine
from policy_model import PolicyEvaluationRequest, PolicyDecision, PolicySource


def _req(action, resource, tenant="t1"):
    return PolicyEvaluationRequest(tenant_id=tenant, user_id="employee:kim", agent_id="agent:assistant:kim", action=action, resource=resource)


def test_default_bundle_id_compatibility():
    b = default_bundle("t1")
    assert b.id == "default-bundle-v1", f"expected default-bundle-v1 got {b.id}"
    # ensure it still carries deterministic small-business rules
    assert any(r.id == "allow-outline-read" for r in b.rules)
    assert any(r.id == "deny-external-export" for r in b.rules)


def test_small_business_bundle_preserves_deterministic_id():
    b = small_business_bundle("t1")
    assert b.id == "small-business-bundle-v1"
    # DELETE everywhere still requires approval via small business profile
    eng = PolicyEngine([b])
    res = eng.evaluate(_req("DELETE", "outline/team/docs"))
    assert res.decision == PolicyDecision.APPROVAL_REQUIRED


def test_delete_unknown_is_default_deny_via_default_bundle():
    b = default_bundle("t1")
    eng = PolicyEngine([b])
    res = eng.evaluate(_req("DELETE", "unknown/resource/123"))
    assert res.decision == PolicyDecision.DENY
    assert res.source == PolicySource.DEFAULT_DENY


def test_delete_outline_still_approval_via_default_bundle():
    b = default_bundle("t1")
    eng = PolicyEngine([b])
    res = eng.evaluate(_req("DELETE", "outline/team/docs"))
    assert res.decision == PolicyDecision.APPROVAL_REQUIRED
    assert res.source == PolicySource.JIT_APPROVAL
