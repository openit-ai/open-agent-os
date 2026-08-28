"""Managed edition VPS installer tests — install dry-run, health-check content, k8s values yaml."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "deploy" / "scripts"
K8S = ROOT / "deploy" / "k8s"


# ── helpers ──────────────────────────────────────────────────────

def run_script(script: Path, *args: str, env: dict | None = None, timeout=20):
    e = os.environ.copy()
    if env:
        e.update(env)
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True, text=True, env=e, timeout=timeout,
    )


# ── installer dry-run ────────────────────────────────────────────

def test_install_sh_exists_and_executable():
    p = SCRIPTS / "install.sh"
    assert p.exists(), f"missing {p}"
    assert os.access(p, os.X_OK)
    txt = p.read_text()
    assert "--dry-run" in txt
    assert "--domain" in txt
    assert "--email" in txt
    assert "--non-interactive" in txt
    assert "docker" in txt.lower()
    assert "hermes" in txt.lower()
    assert "nftables" in txt.lower() or "nft" in txt.lower()
    assert ".env" in txt
    assert "alembic" in txt.lower()
    assert "health" in txt.lower()


def test_install_sh_bash_syntax():
    p = SCRIPTS / "install.sh"
    r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_install_sh_help():
    r = run_script(SCRIPTS / "install.sh", "--help")
    assert r.returncode == 0
    out = r.stdout + r.stderr
    assert "Usage" in out or "usage" in out.lower()
    assert "--domain" in out
    assert "--dry-run" in out


def test_install_sh_dry_run_basic():
    r = run_script(SCRIPTS / "install.sh", "--dry-run")
    assert r.returncode == 0, f"dry-run failed: {r.stderr}\n{r.stdout}"
    combined = r.stdout + r.stderr
    assert "DRY-RUN" in combined
    assert "No changes were made" in combined or "DRY-RUN" in combined


def test_install_sh_dry_run_with_domain_email():
    r = run_script(SCRIPTS / "install.sh", "--dry-run", "--domain", "example.com", "--email", "admin@example.com")
    assert r.returncode == 0, r.stderr
    combined = r.stdout + r.stderr
    assert "DRY-RUN" in combined
    assert "example.com" in combined
    assert "https://example.com" in combined


def test_install_sh_dry_run_non_interactive():
    r = run_script(SCRIPTS / "install.sh", "--dry-run", "--non-interactive", "--domain", "example.com")
    assert r.returncode == 0, r.stderr
    combined = r.stdout + r.stderr
    assert "DRY-RUN" in combined


def test_install_sh_dry_run_env_vars():
    r = run_script(SCRIPTS / "install.sh", "--dry-run", env={"OAOS_DOMAIN": "env.example.com", "OAOS_EMAIL": "env@example.com"})
    assert r.returncode == 0, r.stderr
    assert "env.example.com" in (r.stdout + r.stderr)


# ── health-check script content ──────────────────────────────────

def test_health_check_sh_exists_and_executable():
    p = SCRIPTS / "health-check.sh"
    assert p.exists()
    assert os.access(p, os.X_OK)
    txt = p.read_text()
    assert "8000" in txt
    assert "8001" in txt
    assert "8002" in txt
    assert "postgres" in txt.lower()
    assert "redis" in txt.lower()
    assert "audit" in txt.lower()
    assert "--json" in txt


def test_health_check_sh_bash_syntax():
    r = subprocess.run(["bash", "-n", str(SCRIPTS / "health-check.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_health_check_sh_json_output():
    p = SCRIPTS / "health-check.sh"
    r = subprocess.run(["bash", str(p), "--json"], capture_output=True, text=True, timeout=20)
    # Should exit 0 or 1 depending on services, but must emit valid JSON
    combined = r.stdout.strip()
    # Find json line (stdout should be json)
    assert combined, "health-check --json produced no stdout"
    data = yaml.safe_load(combined) if combined.startswith("{") else None
    import json
    data = json.loads(combined)
    assert "pass" in data
    assert "fail" in data
    assert "results" in data
    assert isinstance(data["results"], list)
    # Check required checks present
    names = [x["name"] for x in data["results"]]
    assert any("control-plane" in n for n in names)
    assert any("postgres" in n.lower() for n in names)
    assert any("redis" in n.lower() for n in names)
    assert any("audit" in n.lower() for n in names)


def test_health_check_sh_help():
    r = run_script(SCRIPTS / "health-check.sh", "--help")
    assert r.returncode == 0
    assert "Usage" in (r.stdout + r.stderr) or "usage" in (r.stdout + r.stderr).lower()


# ── uninstall script ─────────────────────────────────────────────

def test_uninstall_sh_exists_and_executable():
    p = SCRIPTS / "uninstall.sh"
    assert p.exists()
    assert os.access(p, os.X_OK)
    txt = p.read_text()
    assert "--dry-run" in txt
    assert "--keep-data" in txt
    assert "compose" in txt.lower()
    assert "hermes" in txt.lower()


def test_uninstall_sh_bash_syntax():
    r = subprocess.run(["bash", "-n", str(SCRIPTS / "uninstall.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_uninstall_sh_dry_run():
    r = run_script(SCRIPTS / "uninstall.sh", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in (r.stdout + r.stderr)


# ── k8s managed-values.yaml valid yaml ───────────────────────────

def test_managed_values_valid_yaml():
    p = K8S / "managed-values.yaml"
    assert p.exists(), f"missing {p}"
    data = yaml.safe_load(p.read_text())
    assert isinstance(data, dict)


def test_managed_values_has_required_keys():
    data = yaml.safe_load((K8S / "managed-values.yaml").read_text())
    assert "ingress" in data
    assert "replicaCount" in data
    assert "resources" in data
    assert "monitoring" in data
    # ingress
    assert data["ingress"]["enabled"] is True
    assert "host" in data["ingress"]
    assert "tls" in data["ingress"]
    assert data["ingress"]["tls"]["enabled"] is True
    # resources: every service has limits/requests
    for svc in ["controlPlane", "executionGateway", "security", "postgres", "redis"]:
        assert svc in data["resources"], f"missing resources.{svc}"
        assert "limits" in data["resources"][svc]
        assert "requests" in data["resources"][svc]
    # replica counts
    assert data["replicaCount"]["controlPlane"] >= 1
    assert data["replicaCount"]["executionGateway"] >= 1
    assert data["replicaCount"]["security"] >= 1
    # monitoring enabled
    assert data["monitoring"]["enabled"] is True
    assert data["monitoring"]["prometheus"]["enabled"] is True


def test_managed_values_autoscaling():
    data = yaml.safe_load((K8S / "managed-values.yaml").read_text())
    assert "autoscaling" in data
    assert data["autoscaling"]["enabled"] is True


# ── docs ─────────────────────────────────────────────────────────

def test_managed_install_doc_exists():
    # Either managed-install.md or updated deployment-verification
    p1 = ROOT / "docs" / "managed-install.md"
    p2 = ROOT / "docs" / "deployment-verification-2026-08-27.md"
    assert p1.exists() or p2.exists()
    if p1.exists():
        txt = p1.read_text()
        assert "install.sh" in txt
        assert "--domain" in txt
        assert "health-check" in txt
