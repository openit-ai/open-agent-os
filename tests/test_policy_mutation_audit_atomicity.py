"""A policy mutation and its audit record are one transaction.

Regression: `publish()` committed the new version and only then appended the audit
record. When the ledger was unavailable the request returned 500, but the version
stayed published and active — a governance change with no audit trail.
"""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in [
    ROOT,
    ROOT / "security",
    ROOT / "security" / "audit",
    ROOT / "packages" / "audit-model",
    ROOT / "admin-console" / "backend",  # the deployed layout: modules import as top-level
]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

RULES = [
    {
        "id": "deny-external-export",
        "source": "explicit_deny",
        "action": "EXPORT",
        "resource_pattern": "*external*",
        "effect": "DENY",
        "priority": 10,
    },
    {
        "id": "allow-session-interact",
        "source": "default_bundle",
        "action": "INTERACT",
        "resource_pattern": "session/*",
        "effect": "ALLOW",
        "priority": 50,
    },
]


@pytest.fixture()
def policy_db(tmp_path, monkeypatch):
    db = tmp_path / "policy.sqlite"
    monkeypatch.setenv("OAOS_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("OAOS_DATABASE_URL", f"sqlite:///{db}")
    # Re-assert the deployed module layout here: other suites rebuild sys.path.
    backend = str(ROOT / "admin-console" / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    import policy as pol

    pol = importlib.reload(pol)
    engine = pol._db_get_sync_engine()
    pol._ensure_policy_tables_sync(engine)
    engine.dispose()
    yield pol


def _draft(pol, tenant="default"):
    return pol._db_save_draft(
        tenant, "default-bundle-v1", "Default Policy Bundle", RULES, "admin@openit.co.kr", version="draft"
    )


def test_publish_rolls_back_when_the_audit_append_fails(policy_db):
    pol = policy_db
    _draft(pol)

    def failing_audit(connection, record):
        raise RuntimeError("audit ledger unavailable")

    with pytest.raises(RuntimeError, match="audit ledger unavailable"):
        pol._db_publish("default", "admin@openit.co.kr", audit=failing_audit)

    versions = pol._db_list_versions("default")
    assert [v["status"] for v in versions] == ["draft"], "publish must not survive a failed audit"


def test_publish_commits_once_the_audit_succeeds(policy_db):
    pol = policy_db
    _draft(pol)
    seen: list[tuple[str, str]] = []

    def audit(connection, record):
        seen.append((record.get("version"), record.get("bundle_id")))

    published = pol._db_publish("default", "admin@openit.co.kr", audit=audit)

    assert seen == [(published["version"], "default-bundle-v1")]
    assert published["version"] == "1.0.0"
    assert [v["status"] for v in pol._db_list_versions("default")] == ["published"]


def test_rollback_rolls_back_when_the_audit_append_fails(policy_db):
    pol = policy_db
    _draft(pol)
    pol._db_publish("default", "admin@openit.co.kr")
    published_after_first = [v["version"] for v in pol._db_list_versions("default") if v["status"] == "published"]
    assert published_after_first == ["1.0.0"]

    def failing_audit(connection, record):
        raise RuntimeError("audit ledger unavailable")

    with pytest.raises(RuntimeError, match="audit ledger unavailable"):
        pol._db_rollback("1.0.0", "default", "admin@openit.co.kr", audit=failing_audit)

    published = [v["version"] for v in pol._db_list_versions("default") if v["status"] == "published"]
    assert published == ["1.0.0"], "a failed rollback must not add a version"


def test_approve_rolls_back_when_the_audit_append_fails(policy_db):
    pol = policy_db
    _draft(pol)

    def failing_audit(connection, record):
        raise RuntimeError("audit ledger unavailable")

    with pytest.raises(RuntimeError, match="audit ledger unavailable"):
        pol._db_mark_approved("default", "admin@openit.co.kr", audit=failing_audit)

    assert [v["status"] for v in pol._db_list_versions("default")] == ["draft"]
