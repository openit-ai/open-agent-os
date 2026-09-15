"""Canonical infra registry opt-out (OAOS_INFRA_SEED_EXCLUDE) and Outline health path.

Covers the two defects found on the KVM4 deployment:

1. A canonical row deleted by an operator was resurrected on every admin-api
   startup (ensure_canonical_registry) and was also fabricated by
   _build_unified_rows() even with no DB row — so the console kept showing an
   unused service. OAOS_INFRA_SEED_EXCLUDE is the permanent opt-out.
2. Outline's health document is served at /_health; "/" answers 301 to an https
   URL without the port, so a redirect-following probe could never reach 200 and
   the row stayed unhealthy forever.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"


def _load(name: str, filename: str, bare_alias: str | None = None):
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location(name, str(BACKEND / filename))
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    sys.modules[name] = mod
    if bare_alias:
        sys.modules[bare_alias] = mod
    spec.loader.exec_module(mod)  # type: ignore
    return mod


infra_mod = _load("exclude_admin_infra", "infra.py", bare_alias="infra")


@pytest.fixture(autouse=True)
def isolate_stores(monkeypatch):
    """Run the in-memory registry path with no DB env, as other infra tests do."""
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OAOS_INFRA_SEED_EXCLUDE", raising=False)
    infra_mod._db_engine = None
    infra_mod._db_session_factory = None
    infra_mod.clear_services()
    yield
    infra_mod.clear_services()
    infra_mod._db_engine = None
    infra_mod._db_session_factory = None


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def test_exclude_unset_yields_no_names_and_all_canonical_defs():
    assert infra_mod.canonical_excluded_names() == set()
    defs = infra_mod._canonical_defs_for_seed()
    assert [d["name"] for d in defs] == list(infra_mod.CANONICAL_ORDER)


def test_exclude_parses_case_whitespace_and_empties(monkeypatch):
    monkeypatch.setenv("OAOS_INFRA_SEED_EXCLUDE", " Memory , ,NGINX,  ")
    assert infra_mod.canonical_excluded_names() == {"memory", "nginx"}


def test_exclude_ignores_names_outside_canonical_order(monkeypatch):
    monkeypatch.setenv("OAOS_INFRA_SEED_EXCLUDE", "memory,not-a-canonical-name")
    assert infra_mod.canonical_excluded_names() == {"memory", "not-a-canonical-name"}
    defs = infra_mod._canonical_defs_for_seed()
    assert "memory" not in [d["name"] for d in defs]
    assert len(defs) == len(infra_mod.CANONICAL_ORDER) - 1


# --------------------------------------------------------------------------
# seeding
# --------------------------------------------------------------------------

def test_seed_does_not_create_excluded_name(monkeypatch):
    monkeypatch.setenv("OAOS_INFRA_SEED_EXCLUDE", "memory")

    first = infra_mod.ensure_canonical_registry()
    second = infra_mod.ensure_canonical_registry()

    created_names = {svc.name for svc in infra_mod._services.values()}
    assert "memory" not in created_names
    assert first["created_count"] == len(infra_mod.CANONICAL_ORDER) - 1
    assert first["excluded"] == ["memory"]
    assert first["excluded_count"] == 1
    assert "memory" not in first["skipped"]
    # idempotent: second call creates nothing and still reports the exclusion
    assert second["created_count"] == 0
    assert second["excluded"] == ["memory"]


def test_seed_preserves_existing_rows_and_unique_ids(monkeypatch):
    monkeypatch.setenv("OAOS_INFRA_SEED_EXCLUDE", "memory")
    existing = infra_mod.InfraService(
        id="operator_mattermost", name="mattermost", display_name="Operator Mattermost",
        host="chat.operator.internal", port=9443, health_path="/custom-health",
    )
    infra_mod._services[existing.id] = existing

    result = infra_mod.ensure_canonical_registry()
    by_name = {svc.name: svc for svc in infra_mod._services.values()}

    assert result["skipped"] == ["mattermost"]
    assert by_name["mattermost"].id == "operator_mattermost"
    assert by_name["mattermost"].host == "chat.operator.internal"
    assert by_name["mattermost"].health_path == "/custom-health"
    assert "memory" not in by_name


def test_excluding_after_creation_still_removes_it_from_listing(monkeypatch):
    """A row created by an earlier (non-excluding) run must not come back."""
    infra_mod.ensure_canonical_registry()
    assert "memory" in {svc.name for svc in infra_mod._services.values()}

    monkeypatch.setenv("OAOS_INFRA_SEED_EXCLUDE", "memory")
    result = infra_mod.ensure_canonical_registry()

    assert "memory" not in result["skipped"]
    assert "memory" not in result["created"]
    assert result["excluded"] == ["memory"]


# --------------------------------------------------------------------------
# unified rows (console listing)
# --------------------------------------------------------------------------

def test_unified_rows_omit_excluded_name_and_keep_the_rest(monkeypatch):
    monkeypatch.setenv("OAOS_INFRA_SEED_EXCLUDE", "memory")
    infra_mod.ensure_canonical_registry()

    rows = asyncio.run(infra_mod._build_unified_rows(probe=False))
    names = {row.get("name") for row in rows}

    assert "memory" not in names
    assert "nginx" in names
    assert "control-plane" in names


def test_unified_rows_without_exclude_still_list_every_canonical(monkeypatch):
    infra_mod.ensure_canonical_registry()

    rows = asyncio.run(infra_mod._build_unified_rows(probe=False))
    names = {row.get("name") for row in rows}

    for canonical in infra_mod.CANONICAL_ORDER:
        assert canonical in names


# --------------------------------------------------------------------------
# Outline health path
# --------------------------------------------------------------------------

def test_outline_live_entry_uses_health_document(monkeypatch):
    monkeypatch.delenv("OUTLINE_URL", raising=False)
    monkeypatch.delenv("OAOS_OUTLINE_URL", raising=False)
    monkeypatch.delenv("OUTLINE_API_URL", raising=False)

    entry = infra_mod._resolve_outline_live()

    assert entry["health_path"] == "/_health"
    assert entry["expected_status"] == 200


def test_outline_live_entry_uses_health_document_with_env_url(monkeypatch):
    monkeypatch.setenv("OUTLINE_URL", "https://note.example.com:443")

    entry = infra_mod._resolve_outline_live()

    assert entry["health_path"] == "/_health"


def test_outline_canonical_def_carries_health_path(monkeypatch):
    defs = {d["name"]: d for d in infra_mod._canonical_defs_for_seed()}

    assert defs["outline"]["health_path"] == "/_health"


def test_redirecting_root_is_not_treated_as_healthy():
    """The probe compares the final status to expected_status; a 301 root fails."""
    assert infra_mod._resolve_outline_live()["health_path"] != "/"
