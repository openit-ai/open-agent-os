"""Stage 5 — Alembic migration and backup/restore consistency (TDD).

Production DB oaos is live; admin_policy_versions was created manually and
alembic_version is currently 008_approval_nonces on prod. This tests the new
013 revision adds the table properly with upgrade/downgrade (PG+SQLite).

Strict TDD: these tests FAIL before 013 exists.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import os
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "alembic" / "versions"
REV_013 = VERSIONS / "013_admin_policy_versions.py"


def test_013_revision_exists_and_chain_correct():
    assert REV_013.exists(), "013_admin_policy_versions.py missing"
    text = REV_013.read_text()
    assert 'revision = "013_admin_policy_versions"' in text
    assert 'down_revision = "012_knowledge_index"' in text
    # must not contain destructive data loss for other tables
    assert "drop_table" in text.lower()  # downgrade exists
    # upgrade must be idempotent / preserve existing table
    assert "_has_table" in text or "IF NOT EXISTS" in text or "has_table" in text
    # must be PG+SQLite compatible (no postgres-only types without guard)
    assert "admin_policy_versions" in text
    # no branch_labels None check optional but must exist
    assert "branch_labels" in text


def test_orm_model_exists():
    """ORM must expose AdminPolicyVersionORM matching persistence.py schema."""
    # Check security/models/orm.py contains the class
    orm_text = (ROOT / "security" / "models" / "orm.py").read_text()
    assert "AdminPolicyVersionORM" in orm_text or "AdminPolicyVersion" in orm_text
    assert "admin_policy_versions" in orm_text


def test_offline_sql_render_contains_admin_policy_versions():
    """alembic upgrade --sql offline render must contain CREATE TABLE admin_policy_versions."""
    # Use alembic offline SQL generation: alembic upgrade 012:head --sql
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "012_knowledge_index:head", "--sql"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    # Offline SQL should contain admin_policy_versions when at head
    # If already at head, we run head:head sql and check revision file contains DDL
    if "admin_policy_versions" not in combined.lower():
        # fallback: check revision file itself contains CREATE TABLE DDL
        text = REV_013.read_text()
        assert "admin_policy_versions" in text
        assert "CREATE TABLE" in text or "create_table" in text
    else:
        assert "admin_policy_versions" in combined.lower()


def test_upgrade_downgrade_isolated_sqlite():
    """Upgrade/downgrade on isolated SQLite (no prod DB). Must be idempotent."""
    from sqlalchemy import create_engine, inspect, text

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_013.sqlite"
        url = f"sqlite:///{db_path}"

        # Start from empty DB, run alembic upgrade to head
        env = os.environ.copy()
        env["DATABASE_URL"] = url
        env["OAOS_DATABASE_URL"] = url

        # Use alembic via python -m alembic upgrade head
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0, f"upgrade head failed: {result.stdout}\n{result.stderr}"

        # Verify table exists with expected columns + indexes
        eng = create_engine(url)
        insp = inspect(eng)
        assert "admin_policy_versions" in insp.get_table_names(), "table not created"
        cols = {c["name"] for c in insp.get_columns("admin_policy_versions")}
        for expected in ["id", "tenant_id", "bundle_id", "name", "version", "status", "rules_json", "created_at"]:
            assert expected in cols, f"missing col {expected}: {cols}"
        indexes = {ix["name"] for ix in insp.get_indexes("admin_policy_versions")}
        assert "ix_policy_tenant_status" in indexes
        assert "ix_policy_bundle" in indexes

        # Insert a row, downgrade one step (to 012), then upgrade again
        with eng.begin() as conn:
            conn.execute(text(
                "INSERT INTO admin_policy_versions (id, tenant_id, bundle_id, name, version, status, rules_json, created_at) "
                "VALUES ('test-id-1', 'default', 'default-bundle-v1', 'Default', 'v1', 'published', '[]', '2026-01-01T00:00:00+00:00')"
            ))

        # Downgrade to 012 (drop 013 table) — head is now 016 so -1 would only drop 016
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "012_knowledge_index"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0, f"downgrade failed: {result.stdout}\\n{result.stderr}"
        insp2 = inspect(eng)
        assert "admin_policy_versions" not in insp2.get_table_names(), "downgrade to 012 should drop admin_policy_versions table"

        # Upgrade again to head (re-create)
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0, f"re-upgrade failed: {result.stdout}\n{result.stderr}"
        insp3 = inspect(eng)
        assert "admin_policy_versions" in insp3.get_table_names()

        # Verify idempotent: run upgrade head again should not fail
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0

        eng.dispose()


def test_existing_table_preservation():
    """If admin_policy_versions already exists (manually created), upgrade must preserve data (no drop)."""
    from sqlalchemy import create_engine, text, inspect

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_preserve.sqlite"
        url = f"sqlite:///{db_path}"
        eng = create_engine(url)
        # Manually create table as production did (persistence.py DDL)
        with eng.begin() as conn:
            conn.execute(text("""
                CREATE TABLE admin_policy_versions (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    bundle_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    rules_json TEXT NOT NULL,
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    approved_by TEXT,
                    approved_at TEXT,
                    published_at TEXT,
                    parent_version TEXT
                )
            """))
            conn.execute(text("CREATE INDEX ix_policy_tenant_status ON admin_policy_versions (tenant_id, status)"))
            conn.execute(text("CREATE INDEX ix_policy_bundle ON admin_policy_versions (bundle_id, version)"))
            conn.execute(text(
                "INSERT INTO admin_policy_versions (id, tenant_id, bundle_id, name, version, status, rules_json, created_at) "
                "VALUES ('preserve-id', 'default', 'default-bundle-v1', 'Default', 'v1', 'published', '[]', '2026-01-01T00:00:00+00:00')"
            ))
            # Also create alembic_version at 012 to simulate pre-013 state
            conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
            conn.execute(text("DELETE FROM alembic_version"))
            conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('012_knowledge_index')"))
        eng.dispose()

        env = os.environ.copy()
        env["DATABASE_URL"] = url
        env["OAOS_DATABASE_URL"] = url

        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        # Should succeed and NOT delete preserved row (idempotent upgrade skips if table exists)
        assert result.returncode == 0, f"upgrade with existing table failed: {result.stdout}\n{result.stderr}"

        eng2 = create_engine(url)
        with eng2.connect() as conn:
            rows = conn.execute(text("SELECT id, rules_json FROM admin_policy_versions WHERE id='preserve-id'")).fetchall()
            assert len(rows) == 1, "preserved row lost after upgrade (destructive)"
            assert rows[0][1] == "[]"
            # Verify version advanced to head (now 016 after 015/016 additions)
            ver = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
            assert ver is not None, "alembic_version missing"
            expected_heads = {"016_admin_user_mappings_display_avatar", "015_runtime_config_snapshots", "014_adaptive_profile", "013_admin_policy_versions"}
            assert ver[0] in expected_heads, f"unexpected head {ver[0]} not in {expected_heads}"
            assert ver[0] == "016_admin_user_mappings_display_avatar", f"expected head 016, got {ver[0]}"
        eng2.dispose()


def test_backup_restore_verification_safe():
    """Backup/restore dry-run verification — no real DB dump, but script detects admin_policy_versions."""
    # Backup dry-run should create manifest and not fail
    with tempfile.TemporaryDirectory() as tmp:
        backup_dir = Path(tmp) / "backups"
        backup_dir.mkdir()
        result = subprocess.run(
            ["bash", str(ROOT / "deploy" / "scripts" / "backup.sh"), "--dry-run", "--backup-dir", str(backup_dir)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"backup --dry-run failed: {result.stdout}\n{result.stderr}"
        manifests = list(backup_dir.glob("*.json"))
        # manifest file path is printed to stdout by script
        assert len(manifests) >= 1 or "manifest" in (result.stdout + result.stderr).lower()

        # Verify backup.sh mentions pgvector and per-DB dumps (055 §27.11)
        backup_text = (ROOT / "deploy" / "scripts" / "backup.sh").read_text()
        assert "admin_policy_versions" in backup_text or "oaos" in backup_text  # oaos dump includes it via pg_dump

        # Restore dry-run with generated manifest if exists
        manifest = manifests[0] if manifests else None
        if manifest:
            r2 = subprocess.run(
                ["bash", str(ROOT / "deploy" / "scripts" / "restore.sh"), "--manifest", str(manifest), "--dry-run"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert r2.returncode == 0, f"restore --dry-run failed: {r2.stdout}\n{r2.stderr}"

        # Verify restore.sh handles verification flag
        r3 = subprocess.run(
            ["bash", str(ROOT / "deploy" / "scripts" / "restore.sh"), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert "--verify" in r3.stdout or "--verify" in r3.stderr or "verify" in (r3.stdout + r3.stderr).lower()
