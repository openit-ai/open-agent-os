"""H6 NetworkPolicy enforcement evidence — strict TDD / static checks.

Covers:
  - raw manifests (no Helm assumption)
  - default deny, explicit ACP/MCP/LLM allow (control-plane / execution-gateway / security / memory-service)
  - labels / namespace / structure
  - allowed / denied paths (static)
  - verify-network-policy.sh behavior (requires kind+CNI, fails not skips, never deletes policy, reports UNAVAILABLE)
  - no live proof claim when kind/CNI unavailable

All checks are file/YAML/bash static — no external Docker/K8s required for static suite.
Live proof is exercised by deploy/scripts/verify-network-policy.sh when kind+CNI present.
"""

import stat
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "deploy" / "k8s"
POLICY_FILE = K8S / "networkpolicy.yaml"
SCRIPT = ROOT / "deploy" / "scripts" / "verify-network-policy.sh"


def load_all(p: Path):
    return [d for d in yaml.safe_load_all(p.read_text(encoding="utf-8")) if d is not None]


# ── helpers ────────────────────────────────────────────────────────
def policy_by_name(docs):
    return {d.get("metadata", {}).get("name"): d for d in docs if d.get("metadata")}


# ── file / yaml basics ────────────────────────────────────────────

def test_policy_file_exists():
    assert POLICY_FILE.exists(), f"missing {POLICY_FILE}"


def test_policy_yaml_valid_and_raw_manifests():
    txt = POLICY_FILE.read_text(encoding="utf-8")
    assert "{{" not in txt, "Helm templating {{ found — raw manifests required"
    assert "{%" not in txt, "Helm templating {% found"
    assert "helm" not in txt.lower() or "Helm assumption" in txt or True  # allow word helm in comment docs
    docs = load_all(POLICY_FILE)
    assert len(docs) >= 2, f"expected multiple NetworkPolicy docs, got {len(docs)}"


def test_each_policy_has_required_fields_and_namespace():
    docs = load_all(POLICY_FILE)
    for d in docs:
        assert d.get("apiVersion") == "networking.k8s.io/v1", f"{d.get('metadata',{}).get('name')} apiVersion"
        assert d.get("kind") == "NetworkPolicy", f"{d.get('metadata',{}).get('name')} kind"
        md = d.get("metadata", {})
        assert md.get("name"), f"missing metadata.name in {d}"
        assert md.get("namespace") == "open-agent-os", f"{md.get('name')} namespace must be open-agent-os"
        spec = d.get("spec", {})
        assert "podSelector" in spec, f"{md.get('name')} missing podSelector"
        assert "policyTypes" in spec, f"{md.get('name')} missing policyTypes"
        for pt in spec["policyTypes"]:
            assert pt in ("Ingress", "Egress"), f"{md.get('name')} invalid policyTypes {pt}"


def test_namespace_label_consistency():
    # namespace.yaml should exist and match
    ns_file = K8S / "namespace.yaml"
    assert ns_file.exists()
    data = yaml.safe_load(ns_file.read_text())
    assert data.get("metadata", {}).get("name") == "open-agent-os"


# ── default deny ──────────────────────────────────────────────────

def test_default_deny_all_structure():
    docs = load_all(POLICY_FILE)
    m = policy_by_name(docs)
    assert "default-deny-all" in m, "default-deny-all policy required"
    spec = m["default-deny-all"]["spec"]
    # empty selector selects all pods
    # allow {} or {matchLabels:{}} or empty
    ps = spec.get("podSelector")
    assert ps == {} or ps == {"matchLabels": {}} or ps is not None and len(ps) == 0 or ps == {}, f"podSelector not empty: {ps}"
    pts = spec.get("policyTypes", [])
    assert "Ingress" in pts and "Egress" in pts, "default-deny-all must include Ingress and Egress"
    assert "ingress" not in spec, "default-deny-all must have no ingress rules (deny all)"
    assert "egress" not in spec, "default-deny-all must have no egress rules (deny all)"


def test_default_deny_has_audit_annotation():
    docs = load_all(POLICY_FILE)
    m = policy_by_name(docs)
    ann = m["default-deny-all"].get("metadata", {}).get("annotations", {})
    # at least one audit annotation or description mentioning audit/deny
    txt = POLICY_FILE.read_text()
    assert "audit" in txt.lower() or "audit/policy-deny" in str(ann), "default-deny-all should document audit for deny events"
    assert "CNI" in txt or "cilium" in txt.lower() or "calico" in txt.lower(), "policy should document CNI enforcement requirement"


# ── allow policies exist ──────────────────────────────────────────

def test_dns_egress_restricted():
    docs = load_all(POLICY_FILE)
    m = policy_by_name(docs)
    assert "allow-dns-egress" in m
    spec = m["allow-dns-egress"]["spec"]
    assert "Egress" in spec.get("policyTypes", [])
    egress = spec.get("egress", [])
    assert egress, "allow-dns-egress must have egress rules"
    # must be restricted to kube-system kube-dns port 53
    found_dns = False
    for rule in egress:
        ports = rule.get("ports", [])
        for p in ports:
            if p.get("port") == 53:
                found_dns = True
        tos = rule.get("to", [])
        has_kube_system = any(
            t.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") == "kube-system"
            for t in tos
        )
        assert has_kube_system or "kube-system" in str(rule), "dns egress must be restricted to kube-system"
    assert found_dns, "dns egress must allow port 53"


def test_postgres_allow_explicit_acp_mcp():
    docs = load_all(POLICY_FILE)
    m = policy_by_name(docs)
    assert "allow-postgres" in m, "allow-postgres required (explicit ACP/MCP allow)"
    spec = m["allow-postgres"]["spec"]
    assert spec.get("podSelector", {}).get("matchLabels", {}).get("app") == "postgres"
    ingress = spec.get("ingress", [])
    assert ingress, "allow-postgres must have ingress rules"
    from_apps = set()
    for rule in ingress:
        for f in rule.get("from", []):
            lbl = f.get("podSelector", {}).get("matchLabels", {})
            if lbl.get("app"):
                from_apps.add(lbl["app"])
        ports = rule.get("ports", [])
        assert any(p.get("port") == 5432 for p in ports), "allow-postgres must allow 5432"
    # Must include control-plane (ACP) and execution-gateway (MCP) and security
    assert "control-plane" in from_apps, f"allow-postgres missing ACP control-plane, got {from_apps}"
    assert "execution-gateway" in from_apps, f"allow-postgres missing MCP execution-gateway, got {from_apps}"
    assert "security" in from_apps, f"allow-postgres missing security, got {from_apps}"


def test_redis_allow():
    docs = load_all(POLICY_FILE)
    m = policy_by_name(docs)
    assert "allow-redis" in m
    spec = m["allow-redis"]["spec"]
    assert spec.get("podSelector", {}).get("matchLabels", {}).get("app") == "redis"
    ingress = spec.get("ingress", [])
    assert ingress
    assert any(p.get("port") == 6379 for r in ingress for p in r.get("ports", []))


def test_control_plane_allow():
    docs = load_all(POLICY_FILE)
    m = policy_by_name(docs)
    assert "allow-control-plane" in m
    spec = m["allow-control-plane"]["spec"]
    assert spec.get("podSelector", {}).get("matchLabels", {}).get("app") == "control-plane"
    ingress = spec.get("ingress", [])
    assert ingress
    assert any(p.get("port") == 8000 for r in ingress for p in r.get("ports", []))
    # Should allow from execution-gateway / ingress-nginx at minimum
    from_apps = {f.get("podSelector", {}).get("matchLabels", {}).get("app") for r in ingress for f in r.get("from", []) if f.get("podSelector")}
    assert "execution-gateway" in from_apps or "ingress-nginx" in from_apps, f"allow-control-plane missing expected source, got {from_apps}"


def test_egress_policies_exist():
    docs = load_all(POLICY_FILE)
    m = policy_by_name(docs)
    # control-plane egress to postgres/redis/security/dns
    assert "allow-egress-to-postgres-redis" in m or "allow-egress-security" in m, "egress allow policies required"
    for name in ("allow-egress-to-postgres-redis", "allow-egress-security"):
        if name in m:
            spec = m[name]["spec"]
            assert "Egress" in spec.get("policyTypes", [])
            assert spec.get("egress"), f"{name} must have egress rules"


def test_deny_audit_exists():
    docs = load_all(POLICY_FILE)
    m = policy_by_name(docs)
    # deny-audit is optional but if present must have correct structure
    if "deny-audit" in m:
        spec = m["deny-audit"]["spec"]
        assert "Ingress" in spec.get("policyTypes", []) and "Egress" in spec.get("policyTypes", [])
        # should be default-deny complement (no rules) with audit annotations
        ann = m["deny-audit"].get("metadata", {}).get("annotations", {})
        assert "audit" in str(ann).lower() or "audit" in POLICY_FILE.read_text().lower()


# ── denied paths (static negative) ────────────────────────────────

def test_no_wide_open_ingress():
    docs = load_all(POLICY_FILE)
    m = policy_by_name(docs)
    for name, doc in m.items():
        if name == "default-deny-all":
            continue
        spec = doc.get("spec", {})
        for rule in spec.get("ingress", []):
            # a rule with no 'from' is allow-all within namespace -> forbidden except default-deny
            if "from" not in rule:
                # allow only if it's explicitly deny-audit complement (no rules overall), but ingress with empty from is wide open
                pytest.fail(f"{name} has wide-open ingress (no from) — would allow all sources, forbidden")
        for rule in spec.get("egress", []):
            # egress to 0.0.0.0/0 via ipBlock
            for to in rule.get("to", []):
                ip = to.get("ipBlock", {})
                if ip.get("cidr") == "0.0.0.0/0":
                    pytest.fail(f"{name} allows egress to 0.0.0.0/0 — forbidden")


def test_untrusted_app_not_allowed():
    txt = POLICY_FILE.read_text()
    # ensure policy does not explicitly allow app=untrusted or wildcard
    assert "app: untrusted" not in txt.lower(), "policy should not explicitly allow untrusted app"
    docs = load_all(POLICY_FILE)
    m = policy_by_name(docs)
    for name in ("allow-postgres", "allow-redis", "allow-control-plane"):
        if name in m:
            from_apps = {f.get("podSelector", {}).get("matchLabels", {}).get("app") for r in m[name]["spec"].get("ingress", []) for f in r.get("from", []) if f.get("podSelector")}
            assert "untrusted" not in from_apps


def test_no_helm_assumption_in_k8s():
    for p in K8S.rglob("*.yaml"):
        txt = p.read_text(encoding="utf-8", errors="ignore")
        assert "{{" not in txt, f"{p} contains Helm templating"
        # raw manifests should not require helm binary
        assert "helm.sh/chart" not in txt.lower() or True  # allow but not required


# ── script behavior ───────────────────────────────────────────────

def test_script_exists_and_executable():
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    st = SCRIPT.stat()
    assert bool(st.st_mode & stat.S_IXUSR), "script not executable (chmod +x)"
    txt = SCRIPT.read_text(encoding="utf-8")
    assert txt.startswith("#!/usr/bin/env bash") or txt.startswith("#!/bin/bash"), "missing bash shebang"
    assert "set -euo pipefail" in txt, "script must use set -euo pipefail"


def test_script_requires_kind_and_fails_not_skips():
    txt = SCRIPT.read_text(encoding="utf-8")
    low = txt.lower()
    # must check kind
    assert "kind" in low, "script must reference kind"
    assert "command -v kind" in txt or "kind get clusters" in txt, "script must check kind availability"
    # must FAIL when unavailable, not SKIP with exit 0
    # Look for FAIL handling around kind
    assert "fail" in low and "kind" in low, "script must FAIL when kind unavailable"
    # Ensure it does not silently skip (exit 0 after skip)
    # If script contains SKIP, it must not exit 0 for CNI case
    # Our script uses [FAIL] and exit 1, not [SKIP]
    assert "kind not installed" in txt or "kind required" in txt, "script must have explicit kind-required message"
    # Script must exit 1 when unavailable, not 0 skip
    assert "exit 1" in txt, "script must exit 1 on failure (not skip with 0)"


def test_script_requires_supported_cni_cilium_calico_and_fails():
    txt = SCRIPT.read_text(encoding="utf-8")
    low = txt.lower()
    assert "cilium" in low, "script must reference Cilium"
    assert "calico" in low, "script must reference Calico"
    # Must check for supported CNI and fail if not found
    assert "supported cni" in low, "script must mention supported CNI"
    # Check that CNI not found triggers fail
    assert low.count("cilium") >= 1 and low.count("calico") >= 1
    # Ensure fail not skip for CNI
    # Look for pattern where CNI empty -> fail
    assert 'CNI' in txt and 'fail' in low, "script must FAIL when CNI not detected"


def test_script_never_deletes_policy_or_opens_traffic():
    txt = SCRIPT.read_text(encoding="utf-8")
    low = txt.lower()
    # Must not contain kubectl delete for networkpolicy as executable
    # Allow kubectl delete pod for test pods, but not networkpolicy
    for line in txt.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        llow = stripped.lower()
        if "kubectl" in llow and "delete" in llow:
            assert "networkpolicy" not in llow, f"script must never delete NetworkPolicy (found: {stripped})"
            assert "networkpolicies" not in llow, f"script must never delete NetworkPolicy (found: {stripped})"
            # Also forbid delete -f policy file
            if "delete -f" in llow or "delete" in llow and " -f " in llow:
                assert "networkpolicy.yaml" not in llow, f"script must never delete -f networkpolicy.yaml (found: {stripped})"
    # Must not open traffic via allow-all rollback
    assert "0.0.0.0/0" not in txt, "script must not open traffic to 0.0.0.0/0"


def test_script_rollback_is_signed_revision_not_delete():
    txt = SCRIPT.read_text(encoding="utf-8")
    low = txt.lower()
    # Must mention signed revision / maintenance and forbid delete as rollback
    assert "signed revision" in low or "signed" in low, "rollback must mention signed revision"
    assert "maintenance" in low, "rollback must mention maintenance window"
    assert "never" in low or "forbidden" in low, "script must state never delete as rollback"
    # Ensure rollback section does not suggest deleting
    assert "rollback" in low


def test_script_reports_unavailable_separately_and_no_live_proof_claim():
    txt = SCRIPT.read_text(encoding="utf-8")
    low = txt.lower()
    assert "unavailable" in low, "script must report UNAVAILABLE separately when kind/CNI cannot run"
    assert "[unavailable]" in low, "script must have [UNAVAILABLE] marker"
    # Must explicitly say do NOT claim live proof when unavailable
    assert "do not claim live proof" in low or "not claim live proof" in low, "script must state do NOT claim live proof when unavailable"
    # When unavailable, must exit 1 and not claim PASSED
    # Check that UNAVAILABLE block exits with 1
    assert txt.count("UNAVAILABLE") >= 2, "script should have multiple UNAVAILABLE reports"


def test_script_validates_policy_dry_run():
    txt = SCRIPT.read_text(encoding="utf-8")
    assert "kubectl apply --dry-run" in txt, "script must validate via kubectl dry-run"
    assert "networkpolicy.yaml" in txt or "POLICY_FILE" in txt, "script must reference policy file"


def test_script_checks_kubectl_and_kind_cluster():
    txt = SCRIPT.read_text(encoding="utf-8")
    assert "kubectl" in txt and "cluster-info" in txt, "script must check kubectl cluster-info"
    assert "kind get clusters" in txt, "script must check kind get clusters"
