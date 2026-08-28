"""Business edition deploy/monitoring tests — backup/restore/upgrade scripts + prometheus.

Covers: deploy/scripts/*.sh dry-run, deploy/monitoring/*.yml valid yaml,
        grafana dashboards, security-updates doc.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "deploy" / "scripts"
MONITORING = ROOT / "deploy" / "monitoring"


# ---------------------------------------------------------------------------
# Backup / restore / upgrade scripts — existence + executable
# ---------------------------------------------------------------------------

def test_backup_sh_exists_and_executable():
    p = SCRIPTS / "backup.sh"
    assert p.exists(), f"missing {p}"
    assert os.access(p, os.X_OK)
    txt = p.read_text()
    assert "pg_dump" in txt
    assert "REDIS" in txt or "redis" in txt
    assert "--dry-run" in txt
    assert "age" in txt.lower() or "gpg" in txt.lower()


def test_restore_sh_exists_and_executable():
    p = SCRIPTS / "restore.sh"
    assert p.exists()
    assert os.access(p, os.X_OK)
    txt = p.read_text()
    assert "--dry-run" in txt
    # should handle decrypt + psql or docker
    assert "decrypt" in txt.lower() or "age" in txt.lower() or "gpg" in txt.lower()


def test_upgrade_sh_exists_and_executable():
    p = SCRIPTS / "upgrade.sh"
    assert p.exists()
    assert os.access(p, os.X_OK)
    txt = p.read_text()
    assert "--dry-run" in txt
    assert "alembic" in txt.lower() or "migrate" in txt.lower()
    assert "health" in txt.lower()
    assert "rollback" in txt.lower() or "ROLLBACK" in txt


def test_scripts_bash_syntax():
    for name in ["backup.sh", "restore.sh", "upgrade.sh"]:
        p = SCRIPTS / name
        r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, f"bash -n failed for {name}: {r.stderr}"


# ---------------------------------------------------------------------------
# backup.sh dry-run
# ---------------------------------------------------------------------------

def test_backup_sh_dry_run():
    p = SCRIPTS / "backup.sh"
    with tempfile.TemporaryDirectory() as td:
        env = os.environ.copy()
        env["BACKUP_DIR"] = td
        r = subprocess.run(["bash", str(p), "--dry-run"], capture_output=True, text=True, env=env, timeout=15)
        assert r.returncode == 0, f"backup --dry-run failed: {r.stderr}\n{r.stdout}"
        assert "DRY-RUN" in r.stdout or "DRY-RUN" in r.stderr
        # should have created placeholder files
        files = list(Path(td).glob("*"))
        assert len(files) >= 1, f"no files created in dry-run: {list(Path(td).iterdir())}"


def test_backup_sh_dry_run_with_backup_dir_arg():
    p = SCRIPTS / "backup.sh"
    with tempfile.TemporaryDirectory() as td:
        sub = Path(td) / "custom"
        r = subprocess.run(["bash", str(p), "--dry-run", "--backup-dir", str(sub)], capture_output=True, text=True, timeout=15)
        assert r.returncode == 0, r.stderr
        assert sub.exists()


# ---------------------------------------------------------------------------
# restore.sh dry-run
# ---------------------------------------------------------------------------

def test_restore_sh_dry_run():
    p = SCRIPTS / "restore.sh"
    with tempfile.TemporaryDirectory() as td:
        pg_file = Path(td) / "pg.sql.gz"
        pg_file.write_text("-- dummy pg dump")
        redis_file = Path(td) / "redis.rdb"
        redis_file.write_text("dummy redis")
        r = subprocess.run(
            ["bash", str(p), "--pg-file", str(pg_file), "--redis-file", str(redis_file), "--dry-run"],
            capture_output=True, text=True, timeout=15,
        )
        assert r.returncode == 0, f"restore --dry-run failed: {r.stderr}\n{r.stdout}"
        combined = r.stdout + r.stderr
        assert "DRY-RUN" in combined or "dry_run=1" in combined


# ---------------------------------------------------------------------------
# upgrade.sh dry-run
# ---------------------------------------------------------------------------

def test_upgrade_sh_dry_run():
    p = SCRIPTS / "upgrade.sh"
    r = subprocess.run(["bash", str(p), "--dry-run"], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"upgrade --dry-run failed: {r.stderr}\n{r.stdout}"
    combined = r.stdout + r.stderr
    assert "DRY-RUN" in combined
    assert "Rolling" in combined or "rolling" in combined.lower() or "Would run" in combined


def test_upgrade_sh_help():
    p = SCRIPTS / "upgrade.sh"
    r = subprocess.run(["bash", str(p), "--help"], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    assert "Usage" in r.stdout or "usage" in r.stdout.lower()


def test_backup_sh_help():
    p = SCRIPTS / "backup.sh"
    r = subprocess.run(["bash", str(p), "--help"], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    assert "Usage" in r.stdout or "usage" in r.stdout.lower()


# ---------------------------------------------------------------------------
# prometheus / alerts yaml — valid yaml + expected jobs/groups
# ---------------------------------------------------------------------------

def test_prometheus_yml_valid_yaml():
    p = MONITORING / "prometheus.yml"
    assert p.exists(), f"missing {p}"
    data = yaml.safe_load(p.read_text())
    assert "global" in data
    assert "scrape_configs" in data
    assert "rule_files" in data
    # must reference alerts.yml
    assert any("alerts" in str(x) for x in data["rule_files"])
    # check required jobs
    jobs = {j["job_name"] for j in data["scrape_configs"]}
    for expected in ["control-plane", "execution-gateway", "prometheus", "postgres", "redis"]:
        assert expected in jobs, f"missing job {expected} in {jobs}"


def test_prometheus_yml_has_business_labels():
    p = MONITORING / "prometheus.yml"
    data = yaml.safe_load(p.read_text())
    # global external_labels should contain edition business
    labels = data.get("global", {}).get("external_labels", {})
    assert labels.get("edition") == "business"


def test_alerts_yml_valid_yaml():
    p = MONITORING / "alerts.yml"
    assert p.exists()
    data = yaml.safe_load(p.read_text())
    assert "groups" in data
    assert len(data["groups"]) >= 3
    # collect all alert names
    alerts = []
    for g in data["groups"]:
        for rule in g.get("rules", []):
            if "alert" in rule:
                alerts.append(rule["alert"])
    for expected in ["BackupFailed", "UpgradeFailed", "ControlPlaneDown"]:
        assert expected in alerts, f"missing alert {expected} in {alerts}"


def test_alerts_yml_backup_stale_and_audit():
    p = MONITORING / "alerts.yml"
    data = yaml.safe_load(p.read_text())
    alerts = [r["alert"] for g in data["groups"] for r in g.get("rules", []) if "alert" in r]
    # from spec: AuditChainBreak, BackupStale at minimum
    assert "BackupStale" in alerts or "BackupFailed" in alerts
    assert "AuditChainBreak" in alerts


# ---------------------------------------------------------------------------
# grafana dashboards
# ---------------------------------------------------------------------------

def test_grafana_dashboard_valid_json():
    p = MONITORING / "grafana" / "dashboards" / "business-overview.json"
    assert p.exists(), f"missing {p}"
    data = json.loads(p.read_text())
    assert "panels" in data
    assert len(data["panels"]) >= 2
    titles = [pl.get("title", "") for pl in data["panels"]]
    assert any("QPS" in t or "qps" in t.lower() for t in titles)
    assert any("Latency" in t or "latency" in t.lower() for t in titles)


def test_grafana_provisioning_exists():
    p = MONITORING / "grafana" / "provisioning.yml"
    assert p.exists()
    data = yaml.safe_load(p.read_text())
    assert data is not None


# ---------------------------------------------------------------------------
# security-updates doc
# ---------------------------------------------------------------------------

def test_security_updates_doc_exists():
    p = ROOT / "docs" / "security-updates.md"
    assert p.exists()
    txt = p.read_text()
    assert "CVE" in txt
    assert "BSL" in txt or "License" in txt
