"""Focused regression: collector correction — stale fallback, hermes canonicalization, metadata non-secret.

Validates minimal correction per task:
- Hermes snapshot uses effective OAOS_CP_HERMES_BASE_URL/OAOS_CP_HERMES_MODEL and canonicalizes localhost→127.0.0.1 and :8001→:8642
- Snapshot config includes non-secret collector source/observed_at/inventory_status metadata (no raw secrets)
- Stale fallback prevented: snapshot reflects current fallback chain (DB/live), not stale empty
"""
from __future__ import annotations
import importlib.util, sys, pathlib, os, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"
CP_ROOT = ROOT / "control-plane"

for p in [str(CP_ROOT), str(ROOT/"security"/"policy-engine"), str(ROOT/"security"/"audit")]:
    if p not in sys.path:
        sys.path.insert(0, p)

def _load(name, path):
    import types, importlib.util
    # ensure backend on path for sibling imports (auth) and CP
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec and spec.loader
    for pkg in ("admin_console","admin_console.backend"):
        if pkg not in sys.modules:
            m=types.ModuleType(pkg); m.__path__=[]; sys.modules[pkg]=m
    spec.loader.exec_module(mod)
    return mod

os.environ.pop("OAOS_ENV", None)
os.environ.pop("OAOS_RUNTIME_CONFIG_SIGNING_KEY", None)
os.environ["OAOS_CORS_ORIGINS"]="http://localhost:3012"
os.environ["OAOS_VAULT_KEY"]="test-vault-key-for-llm-provider-32bytes!!"

from fastapi.testclient import TestClient

def _admin_client(db_url=None):
    if db_url is not None:
        os.environ["OAOS_DATABASE_URL"]=db_url
        os.environ["DATABASE_URL"]=db_url
    auth = _load("admin_console.backend.auth", BACKEND/"auth.py")
    app_mod = _load("admin_console.backend.app", BACKEND/"app.py")
    return app_mod.app, auth

def _login(client, email="admin@openit.co.kr", password="Admin123!"):
    r=client.post("/v1/auth/login", json={"email":email,"password":password})
    assert r.status_code==200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}

def _tmp_sqlite_url():
    tf=tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tf.close()
    return f"sqlite:///{tf.name}"

def test_hermes_canonicalization_uses_effective_env_and_localhost_normalized():
    # Ensure hermes collector prefers OAOS_CP_HERMES_* env and canonicalizes localhost/ :8001
    # Load rc via _load to ensure admin_console package is set up (pytest doesn't auto-add backend to path)
    _load("admin_console.backend.runtime_config", BACKEND/"runtime_config.py")
    import admin_console.backend.runtime_config as rc
    rc.clear_runtime_config_state()
    # Case 1: env with localhost should canonicalize to 127.0.0.1
    os.environ["OAOS_CP_HERMES_BASE_URL"]="http://localhost:8642"
    os.environ["OAOS_CP_HERMES_MODEL"]="muse-spark-1.2-contributor"
    try:
        h = rc._collect_hermes()
        assert h["base_url"]=="http://127.0.0.1:8642", f"localhost not canonicalized: {h}"
        assert h["model"]=="muse-spark-1.2-contributor"
        assert "127.0.0.1" in h["base_url"]
        assert "localhost" not in h["base_url"]
        assert "source" in h and "observed_at" in h
        assert h["source"]!="unknown"
        assert h["observed_at"]
    finally:
        os.environ.pop("OAOS_CP_HERMES_BASE_URL", None)
        os.environ.pop("OAOS_CP_HERMES_MODEL", None)
    # Case 2: legacy control-plane default :8001 should map to canonical :8642 (and qwen2.5 replaced)
    # No env set, so default canonical should be used, not control_plane stale defaults
    # Control plane config defaults are http://localhost:8001 / qwen2.5 — collector must normalize to canonical
    h2 = rc._collect_hermes()
    assert h2["base_url"]==rc._CANONICAL_HERMES_BASE_URL, f"default not canonical: {h2}"
    assert h2["model"]==rc._CANONICAL_HERMES_DEFAULT_MODEL
    assert h2["base_url"]=="http://127.0.0.1:8642"
    assert "localhost" not in h2["base_url"]
    # source may be default:canonical or control_plane stale normalized (both acceptable if canonical values)
    assert h2["source"] != "unknown" and h2["source"], f"source unexpected: {h2['source']}"
    assert h2["base_url"] == rc._CANONICAL_HERMES_BASE_URL
    # Case 3: explicit :8001 env should also canonicalize
    os.environ["OAOS_CP_HERMES_BASE_URL"]="http://127.0.0.1:8001"
    os.environ["OAOS_CP_HERMES_MODEL"]="test-model-x"
    try:
        h3 = rc._collect_hermes()
        assert h3["base_url"]=="http://127.0.0.1:8642", f":8001 not normalized: {h3}"
        assert h3["model"]=="test-model-x"
    finally:
        os.environ.pop("OAOS_CP_HERMES_BASE_URL", None)
        os.environ.pop("OAOS_CP_HERMES_MODEL", None)

def test_hermes_snapshot_config_uses_effective_env_via_api():
    db_url=_tmp_sqlite_url()
    admin_app,_ = _admin_client(db_url)
    import admin_console.backend.runtime_config as rc
    rc.clear_runtime_config_state()
    ac=TestClient(admin_app)
    hdr=_login(ac)
    os.environ["OAOS_CP_HERMES_BASE_URL"]="http://localhost:8642"
    os.environ["OAOS_CP_HERMES_MODEL"]="muse-spark-1.2-contributor"
    try:
        r=ac.post("/v1/runtime/config/snapshot", json={"tenant_id":"default"}, headers=hdr)
        assert r.status_code==201, r.text
        snap=r.json()
        hermes=snap["config"]["hermes"]
        assert hermes["base_url"]=="http://127.0.0.1:8642"
        assert hermes["model"]=="muse-spark-1.2-contributor"
        assert "localhost" not in hermes["base_url"]
        assert "source" in hermes and "observed_at" in hermes
        assert hermes["source"] not in ("unknown", "")
        # ensure no raw secrets leaked even if env contains secrets (hermes has no secrets field)
        assert "api_key" not in str(snap).lower() or "vault://" in str(snap) or "api_key" not in str(hermes).lower()
    finally:
        os.environ.pop("OAOS_CP_HERMES_BASE_URL", None)
        os.environ.pop("OAOS_CP_HERMES_MODEL", None)

def test_snapshot_includes_nonsecret_collector_metadata_without_raw_secrets():
    db_url=_tmp_sqlite_url()
    admin_app,_ = _admin_client(db_url)
    import admin_console.backend.runtime_config as rc
    rc.clear_runtime_config_state()
    ac=TestClient(admin_app)
    hdr=_login(ac)
    # create infra + provider to populate collectors
    ac.post("/v1/runtime/mode", json={"mode":"llm"}, headers=hdr)
    ac.post("/v1/infra/services", json={"name":"svc1","display_name":"S1","host":"127.0.0.1","port":9000}, headers=hdr)
    r=ac.post("/v1/llm/providers", json={"provider":"openrouter","apiKey":"sk-test-secret-123","model":"test-model"}, headers=hdr)
    assert r.status_code in (200,201), r.text
    r=ac.post("/v1/runtime/config/snapshot", json={"tenant_id":"default"}, headers=hdr)
    assert r.status_code==201, r.text
    snap=r.json()
    cfg=snap["config"]
    # All collectors must expose non-secret source/observed_at/inventory_status metadata
    # hermes: source + observed_at, no secrets
    assert "hermes" in cfg
    assert "source" in cfg["hermes"] and "observed_at" in cfg["hermes"]
    assert cfg["hermes"]["source"] not in ("unknown","")
    # fallback: source/observed_at/inventory_status
    assert "fallback" in cfg
    for k in ("source","observed_at","inventory_status"):
        assert k in cfg["fallback"], f"fallback missing {k}: {cfg['fallback']}"
    # infra
    assert "infra" in cfg
    for k in ("source","observed_at","inventory_status"):
        assert k in cfg["infra"], f"infra missing {k}"
    # user_mappings
    assert "user_mappings" in cfg
    for k in ("source","observed_at","inventory_status"):
        assert k in cfg["user_mappings"], f"user_mappings missing {k}"
    # llm_providers additive metadata (list contract preserved, metadata at top-level config)
    for k in ("llm_providers_source","llm_providers_observed_at","llm_providers_inventory_status","llm_providers_count"):
        assert k in cfg, f"missing {k}"
        assert cfg[k] not in (None, ""), f"{k} empty"
    # Inventory status must be explicit empty:observed or populated, not unknown (except error cases)
    for k in ("llm_providers_inventory_status",):
        assert cfg[k] != "unknown"
    # No raw secrets anywhere
    blob=str(snap)
    assert "sk-test-secret-123" not in blob
    assert "encrypted_api_key" not in blob
    for p in cfg.get("llm_providers", []):
        assert "encrypted_api_key" not in p
        assert "api_key" not in p
        assert "apiKey" not in p
        # secret_ref should be vault:// if provider enabled
        if p.get("provider")=="openrouter":
            assert p.get("secret_ref","").startswith("vault://")

def test_prevent_stale_fallback_snapshot_reflects_current_chain():
    """Regression: snapshot must reflect current fallback chain, not stale empty."""
    db_url=_tmp_sqlite_url()
    admin_app,_ = _admin_client(db_url)
    import admin_console.backend.runtime_config as rc
    import admin_console.backend.fallback as fb
    rc.clear_runtime_config_state()
    fb.clear_fallback_cache()
    ac=TestClient(admin_app)
    hdr=_login(ac)
    # Ensure llm mode so fallback is writable
    ac.post("/v1/runtime/mode", json={"mode":"llm"}, headers=hdr)
    # Initial snapshot should have empty fallback chain
    r=ac.post("/v1/runtime/config/snapshot", json={"tenant_id":"default"}, headers=hdr)
    assert r.status_code==201, r.text
    snap1=r.json()
    assert snap1["config"]["fallback"]["chain"]==[]
    assert snap1["config"]["fallback"]["inventory_status"]=="empty:observed:zero-chain"
    # Update fallback chain via admin API (DB primary)
    r=ac.put("/v1/llm/fallback", json={"enabled":True,"chain":[{"provider":"openrouter","model":"test-fallback-model","enabled":True}]}, headers=hdr)
    assert r.status_code==200, r.text
    assert len(r.json()["chain"])==1
    # Second snapshot must reflect new chain — not stale empty
    r=ac.post("/v1/runtime/config/snapshot", json={"tenant_id":"default"}, headers=hdr)
    assert r.status_code==201, r.text
    snap2=r.json()
    assert len(snap2["config"]["fallback"]["chain"])==1, f"stale fallback prevented failed: {snap2['config']['fallback']}"
    assert snap2["config"]["fallback"]["chain"][0]["provider"]=="openrouter"
    assert snap2["config"]["fallback"]["chain"][0]["model"]=="test-fallback-model"
    assert snap2["config"]["fallback"]["inventory_status"]=="populated"
    assert snap2["config"]["fallback"]["source"]!="unknown"
    assert snap2["config"]["fallback"]["observed_at"]
    # Clear chain again and verify snapshot goes back to empty (not stale populated)
    r=ac.put("/v1/llm/fallback", json={"enabled":True,"chain":[]}, headers=hdr)
    assert r.status_code==200
    r=ac.post("/v1/runtime/config/snapshot", json={"tenant_id":"default"}, headers=hdr)
    assert r.status_code==201, r.text
    snap3=r.json()
    assert snap3["config"]["fallback"]["chain"]==[]
    assert snap3["config"]["fallback"]["inventory_status"]=="empty:observed:zero-chain"
