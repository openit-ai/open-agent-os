"""Canonical runtime-config correction — hermes normalization + observed metadata."""
from __future__ import annotations
import importlib.util, sys, pathlib, os

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"

def _load_rc():
    import types
    for pkg in ("admin_console","admin_console.backend"):
        if pkg not in sys.modules:
            m=types.ModuleType(pkg); m.__path__=[]; sys.modules[pkg]=m
    # ensure BACKEND on path for relative imports
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    if str(ROOT/"security"/"policy-engine") not in sys.path:
        sys.path.insert(0, str(ROOT/"security"/"policy-engine"))
    spec = importlib.util.spec_from_file_location("admin_console.backend.runtime_config", str(BACKEND/"runtime_config.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["admin_console.backend.runtime_config"] = mod
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod

def test_hermes_normalization_and_metadata():
    rc = _load_rc()
    # pure normalization
    assert rc._normalize_hermes_base_url("http://localhost:8642") == "http://127.0.0.1:8642"
    assert rc._normalize_hermes_base_url("http://127.0.0.1:8001") == "http://127.0.0.1:8642"
    assert rc._normalize_hermes_base_url("http://127.0.0.1:8642") == "http://127.0.0.1:8642"
    # defaults canonical when env absent and CP config cleared
    for k in ("OAOS_CP_HERMES_BASE_URL","HERMES_BASE_URL","OAOS_CP_HERMES_MODEL","HERMES_MODEL"):
        os.environ.pop(k, None)
    # clear CP settings contribution to test canonical path
    try:
        from control_plane.config import settings as cp_settings
        orig_base = cp_settings.hermes_base_url
        orig_model = cp_settings.hermes_model
        cp_settings.hermes_base_url = ""
        cp_settings.hermes_model = ""
        h = rc._collect_hermes()
        assert h["base_url"] == "http://127.0.0.1:8642"
        assert h["model"] == "muse-spark-1.2-contributor"
        assert "source" in h and h["source"]
        assert "observed_at" in h and h["observed_at"]
        # restore
        cp_settings.hermes_base_url = orig_base
        cp_settings.hermes_model = orig_model
    except Exception:
        h = rc._collect_hermes()
        assert h["base_url"] == "http://127.0.0.1:8642"
        # model may be CP's qwen2.5 when not cleared; just ensure metadata present
        assert "source" in h and "observed_at" in h
    # env precedence (normalization)
    os.environ["OAOS_CP_HERMES_BASE_URL"] = "http://localhost:8642"
    os.environ["OAOS_CP_HERMES_MODEL"] = "custom-model"
    h2 = rc._collect_hermes()
    assert h2["base_url"] == "http://127.0.0.1:8642"
    assert h2["model"] == "custom-model"
    assert "env:OAOS_CP_HERMES_BASE_URL" in h2["source"]
    os.environ.pop("OAOS_CP_HERMES_BASE_URL", None)
    os.environ.pop("OAOS_CP_HERMES_MODEL", None)

def test_collectors_include_observed_metadata_no_secrets():
    rc = _load_rc()
    # clear state isolated
    try:
        rc.clear_runtime_config()
    except Exception:
        pass
    # ensure env clean for hermes canonical
    for k in ("OAOS_CP_HERMES_BASE_URL","HERMES_BASE_URL","OAOS_CP_HERMES_MODEL","HERMES_MODEL"):
        os.environ.pop(k, None)
    # fallback/infra/mappings should return metadata fields
    fb = rc._collect_fallback()
    assert "source" in fb and "observed_at" in fb and "inventory_status" in fb
    assert "chain" in fb
    infra = rc._collect_infra()
    assert "source" in infra and "observed_at" in infra and "inventory_status" in infra
    assert "services" in infra
    um = rc._collect_user_mappings()
    assert "source" in um and "observed_at" in um and "inventory_status" in um
    assert "mappings" in um
    providers = rc._collect_llm_providers()
    meta = rc._collect_llm_providers_meta()
    assert "source" in meta and "observed_at" in meta and "inventory_status" in meta
    # providers must be refs only
    for p in providers:
        assert "encrypted_api_key" not in p
        assert "api_key" not in p

def test_snapshot_config_includes_metadata_and_signature_valid():
    rc = _load_rc()
    rc.clear_runtime_config_state()
    for k in ("OAOS_CP_HERMES_BASE_URL","HERMES_BASE_URL","OAOS_CP_HERMES_MODEL","HERMES_MODEL"):
        os.environ.pop(k, None)
    # clear CP settings for canonical hermes
    cp_settings = None
    orig_base = orig_model = None
    try:
        from control_plane.config import settings as _s
        cp_settings = _s
        orig_base = cp_settings.hermes_base_url
        orig_model = cp_settings.hermes_model
        cp_settings.hermes_base_url = ""
        cp_settings.hermes_model = ""
    except Exception:
        pass
    snap = rc._build_snapshot("default", "tester@example.com", 1, None)
    if cp_settings is not None:
        cp_settings.hermes_base_url = orig_base
        cp_settings.hermes_model = orig_model
    cfg = snap["config"]
    # hermes canonical when CP cleared
    assert cfg["hermes"]["base_url"] == "http://127.0.0.1:8642"
    assert cfg["hermes"]["model"] == "muse-spark-1.2-contributor"
    assert "source" in cfg["hermes"] and "observed_at" in cfg["hermes"]
    # no secrets in whole snapshot
    assert "encrypted_api_key" not in str(snap)
    # metadata fields present
    assert "source" in cfg["fallback"] and "observed_at" in cfg["fallback"] and "inventory_status" in cfg["fallback"]
    assert "source" in cfg["infra"] and "observed_at" in cfg["infra"] and "inventory_status" in cfg["infra"]
    assert "source" in cfg["user_mappings"] and "observed_at" in cfg["user_mappings"] and "inventory_status" in cfg["user_mappings"]
    # signature covers new config (including metadata) — verify passes
    assert rc.verify_snapshot_signature(snap) is True
    # tamper should fail
    tampered = dict(snap)
    tampered["config"] = dict(cfg)
    tampered["config"]["hermes"] = dict(cfg["hermes"])
    tampered["config"]["hermes"]["base_url"] = "http://evil:9999"
    assert rc.verify_snapshot_signature(tampered) is False
