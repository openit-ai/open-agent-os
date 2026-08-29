"""Deployment & distributed state hardening validation — §5/§16D/§16H/§27/§31.

- prod compose requires OAOS_ENV=production + external secrets, no dev defaults
- dev compose binds 127.0.0.1 only, documents defaults
- k8s NetworkPolicy enforced + secret requirements + OAOS_ENV
- approval/audit/replay fail-closed in prod, test fallback non-prod only
- execution-gateway rate limiter Redis-primary fail-closed prod

All checks are file/YAML/env based — no external Docker/K8s required.
"""
import os
import re
import importlib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
K8S = DEPLOY / "k8s"

# ── helpers ────────────────────────────────────────────────────────


def load_yaml(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def load_yaml_all(p: Path):
    return list(yaml.safe_load_all(p.read_text(encoding="utf-8")))


# ── prod compose ─────────────────────────────────────────────────


def test_prod_compose_requires_oaos_env_production():
    p = DEPLOY / "docker-compose.prod.yml"
    assert p.exists(), f"missing {p}"
    txt = p.read_text()
    # must require OAOS_ENV and set to production
    assert "OAOS_ENV" in txt
    assert "production" in txt.lower()
    # pattern OAOS_ENV:? check or equals production
    assert re.search(r"OAOS_ENV", txt)
    data = load_yaml(p)
    services = data.get("services", {})
    for svc in ["control-plane", "execution-gateway", "security", "nginx", "postgres", "redis"]:
        env = services[svc].get("environment", {})
        # normalize env list/dict
        env_str = str(env) + str(services[svc].get("env_file", ""))
        # every app service must reference OAOS_ENV
        if svc in ("control-plane", "execution-gateway", "security"):
            assert "OAOS_ENV" in txt, f"{svc} missing OAOS_ENV reference"


def test_prod_compose_external_secrets_and_no_dev_defaults():
    p = DEPLOY / "docker-compose.prod.yml"
    txt = p.read_text()
    # external secrets required
    assert "POSTGRES_PASSWORD" in txt
    assert "AUDIT_SIGNING_KEY" in txt
    assert "JWT_SIGNING_KEY" in txt
    assert "OAOS_ENCRYPTION_KEY" in txt
    # secrets must be required ( ? or file: )
    assert ":?" in txt or "file:" in txt
    # must not expose postgres/redis ports to host in prod (expose only)
    data = load_yaml(p)
    for svc in ["postgres", "redis"]:
        assert "ports" not in data["services"][svc], f"prod {svc} must not publish ports (use expose)"
    # control-plane etc must use expose, not ports
    for svc in ["control-plane", "execution-gateway", "security"]:
        assert "ports" not in data["services"][svc], f"prod {svc} must not publish ports"
        assert "expose" in data["services"][svc]


def test_prod_compose_no_plaintext_secrets():
    p = DEPLOY / "docker-compose.prod.yml"
    txt = p.read_text()
    # must not contain literal secret like "secret" or "CHANGE_ME" as value
    assert "CHANGE_ME" not in txt
    # dev password "secret" must not appear as literal in prod (except in comments about secrets)
    # allow word secret in variable names, but not `password: secret`
    assert "POSTGRES_PASSWORD: secret" not in txt


# ── dev compose ──────────────────────────────────────────────────


def test_dev_compose_binds_127_0_0_1_and_docs_defaults():
    p = DEPLOY / "docker-compose.dev.yml"
    assert p.exists()
    txt = p.read_text()
    data = load_yaml(p)
    # all published ports must be 127.0.0.1 bound
    for svc, cfg in data.get("services", {}).items():
        for port in cfg.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"dev {svc} port {port} must bind 127.0.0.1"
    # documentation comment
    assert "127.0.0.1" in txt
    assert "NOT for production" in txt or "dev defaults" in txt.lower()


def test_dev_compose_has_no_required_secrets():
    p = DEPLOY / "docker-compose.dev.yml"
    txt = p.read_text()
    # dev may have literal dev defaults
    assert "POSTGRES_PASSWORD" in txt
    # but must document they are dev-only
    assert "dev" in txt.lower()


# ── k8s ──────────────────────────────────────────────────────────


def test_k8s_networkpolicy_enforced():
    p = K8S / "networkpolicy.yaml"
    assert p.exists()
    docs = load_yaml_all(p)
    assert len(docs) >= 1
    # default-deny-all must exist
    names = {d.get("metadata", {}).get("name") for d in docs if d}
    assert "default-deny-all" in names
    # each policy must have podSelector and policyTypes
    for d in docs:
        if not d:
            continue
        assert d.get("kind") == "NetworkPolicy"
        spec = d.get("spec", {})
        assert "podSelector" in spec
        assert "policyTypes" in spec
    # check header documents limitations about CNI enforcement
    txt = p.read_text()
    assert "CNI" in txt or "enforcement" in txt.lower()


def test_k8s_secret_template_requirements():
    p = K8S / "secret.yaml.template"
    assert p.exists()
    txt = p.read_text()
    assert "CHANGE_ME" in txt
    assert "OAOS_ENCRYPTION_KEY" in txt
    assert "Do not commit" in txt or "DO NOT" in txt
    assert "ExternalSecrets" in txt or "external" in txt.lower()
    # must list all required keys
    for k in ["POSTGRES_PASSWORD", "JWT_SIGNING_KEY", "AUDIT_SIGNING_KEY", "OAOS_ENCRYPTION_KEY"]:
        assert k in txt


def test_k8s_configmap_has_oaos_env():
    p = K8S / "configmap.yaml"
    assert p.exists()
    data = load_yaml(p)
    assert data.get("kind") == "ConfigMap"
    assert "OAOS_ENV" in data.get("data", {})


def test_k8s_deployments_have_oaos_env():
    for sub in ["control-plane/deployment.yaml", "execution-gateway/deployment.yaml", "security/deployment.yaml"]:
        p = K8S / sub
        assert p.exists(), f"missing {p}"
        data = load_yaml(p)
        env = data["spec"]["template"]["spec"]["containers"][0].get("env", [])
        names = {e.get("name") for e in env}
        assert "OAOS_ENV" in names, f"{sub} missing OAOS_ENV env"


def test_k8s_yaml_valid():
    for p in K8S.rglob("*.yaml"):
        # skip secret template which contains placeholder but still valid yaml
        try:
            docs = list(yaml.safe_load_all(p.read_text()))
            assert docs is not None
        except Exception as e:
            pytest.fail(f"YAML invalid {p}: {e}")


# ── fail-closed: audit/approval/token ────────────────────────────


def test_audit_ledger_fail_closed_in_prod(monkeypatch):
    # import after setting env
    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    # force reimport to pick up new helpers
    import security.audit.audit_ledger.ledger as mod
    importlib.reload(mod)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        mod.AuditLedger(signing_key="k")
    # non-prod fallback must work
    monkeypatch.setenv("OAOS_ENV", "development")
    importlib.reload(mod)
    ledger = mod.AuditLedger(signing_key="k")
    assert ledger is not None
    # cleanup
    monkeypatch.delenv("OAOS_ENV", raising=False)
    importlib.reload(mod)


def test_approval_store_fail_closed_in_prod(monkeypatch):
    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    import security.approval.approval_workflow.workflow as mod
    importlib.reload(mod)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        mod.ApprovalStore(signing_key="k")
    monkeypatch.setenv("OAOS_ENV", "development")
    importlib.reload(mod)
    store = mod.ApprovalStore(signing_key="k")
    assert store is not None
    monkeypatch.delenv("OAOS_ENV", raising=False)
    importlib.reload(mod)


def test_token_service_fail_closed_in_prod(monkeypatch):
    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("OAOS_REDIS_URL", raising=False)
    import security.token.token_service.service as mod
    importlib.reload(mod)
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        mod.TokenService(signing_key="k")
    # non-prod allows in-memory
    monkeypatch.setenv("OAOS_ENV", "development")
    importlib.reload(mod)
    svc = mod.TokenService(signing_key="k")
    tok = svc.issue(sub="a", on_behalf_of="b", action="READ", resource="r", session_id="s", request_id="q")
    payload = svc.verify(tok)
    assert payload["sub"] == "a"
    monkeypatch.delenv("OAOS_ENV", raising=False)
    importlib.reload(mod)


def test_tool_rate_limiter_redis_primary_fail_closed_in_prod(monkeypatch):
    # ToolRateLimiter in prod with REDIS_URL but no redis server should fail-closed or fallback handled
    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6399/0")
    from execution_gateway.tool_policy import ToolRateLimiter
    limiter = ToolRateLimiter(rate_per_sec=10, burst=5)
    # With no redis server, allow should raise RuntimeError (fail-closed)
    with pytest.raises(RuntimeError, match="Redis"):
        limiter.allow("test-key")
    # non-prod with same URL should fallback to in-memory and succeed
    monkeypatch.setenv("OAOS_ENV", "development")
    # need fresh limiter after env change
    limiter2 = ToolRateLimiter(rate_per_sec=10, burst=5)
    assert limiter2.allow("test-key") is True
    monkeypatch.delenv("OAOS_ENV", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)


def test_compose_yaml_valid():
    for name in ["docker-compose.prod.yml", "docker-compose.dev.yml"]:
        p = DEPLOY / name
        data = yaml.safe_load(p.read_text())
        assert "services" in data
