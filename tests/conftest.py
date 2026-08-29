import sys
import os
from pathlib import Path
# Ensure control-plane + packages are importable without manual PYTHONPATH (Workstream A+B+C)
ROOT = Path(__file__).resolve().parents[1]
for p in [
    ROOT / "control-plane",
    ROOT / "execution-gateway",
    ROOT / "security/policy-engine",
    ROOT / "security/delegation",
    ROOT / "security/credential-vault",
    ROOT / "security/crypto",
    ROOT / "security/audit",
    ROOT / "security/approval",
    ROOT / "security/memory-governance",
    ROOT / "security/token",
    ROOT / "packages/common-types",
    ROOT / "packages/agent-context",
    ROOT / "packages/policy-model",
    ROOT / "packages/audit-model",
    ROOT / "packages/delegation-model",
    ROOT / "packages/mcp-resource-model",
    ROOT / "packages/runtime-adapter",
    ROOT / "packages/personal-wiki",
    ROOT / "packages/agent-runtime",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
# security/token collides with stdlib 'token' — need parent 'security' on path for legacy `import token.token_service`
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "security") not in sys.path:
    sys.path.insert(0, str(ROOT / "security"))

# === Unified test signing key contract (C1/H1/H2/H3) ===
UNIFIED_TEST_KEY = "test-unified-oaos-signing-key-32bytes-long-enough!!"
for _k in (
    "OAOS_SIGNING_KEY",
    "OAOS_SECURITY_SERVICE_SIGNING_KEY",
    "OAOS_USER_JWT_SIGNING_KEY",
    "OAOS_JWT_SIGNING_KEY",
    "OAOS_AGENT_CONTEXT_SIGNING_KEY",
    "OAOS_AGENT_JWT_SIGNING_KEY",
    "OAOS_SIGNED_CONTEXT_SIGNING_KEY",
    "JWT_SIGNING_KEY",
    "ADMIN_JWT_SECRET",
    "OAOS_WIKI_JWT_SIGNING_KEY",
):
    os.environ[_k] = UNIFIED_TEST_KEY

os.environ.setdefault("OAOS_USER_JWT_ISSUER", "open-agent-os-auth")
os.environ.setdefault("OAOS_JWT_ISSUER", "open-agent-os-auth")
os.environ.setdefault("OAOS_AUTH_ISSUER", "open-agent-os-auth")
os.environ.setdefault("OAOS_USER_JWT_AUDIENCE", "control-plane")
os.environ.setdefault("OAOS_JWT_AUDIENCE", "control-plane")
os.environ.setdefault("OAOS_AUTH_AUDIENCE", "control-plane")
os.environ.setdefault("OAOS_AGENT_CONTEXT_ISSUER", "control-plane")
os.environ.setdefault("OAOS_SIGNED_CONTEXT_ISSUER", "control-plane")
os.environ.setdefault("OAOS_AGENT_JWT_ISSUER", "control-plane")
os.environ.setdefault("OAOS_AGENT_CONTEXT_AUDIENCE", "execution-gateway")
os.environ.setdefault("OAOS_SIGNED_CONTEXT_AUDIENCE", "execution-gateway")
os.environ.setdefault("OAOS_AGENT_JWT_AUDIENCE", "execution-gateway")
os.environ.pop("OAOS_ENV", None)

import pytest

_ADMIN_ENV_SNAPSHOT_KEYS = (
    "OAOS_RUNTIME_MODE",
    "OAOS_DATABASE_URL",
    "DATABASE_URL",
    "OAOS_VAULT_KEY",
    "VAULT_ENCRYPTION_KEY",
    "OAOS_ENV",
    "OAOS_ENFORCE_SIGNED_CONTEXT",
    "OAOS_TEST_ALLOW_PLAINTEXT",
    "PYTEST_CURRENT_TEST",
)

def _snapshot_env():
    return {k: os.environ.get(k) for k in _ADMIN_ENV_SNAPSHOT_KEYS}

def _restore_env(snap):
    for k in _ADMIN_ENV_SNAPSHOT_KEYS:
        v = snap.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

def _canonical_llm_module():
    for name in ("admin_console.backend.llm_providers", "llm_providers"):
        m = sys.modules.get(name)
        if m is not None:
            return m
    return None

def _clear_canonical_admin_state():
    """Clear canonical admin_console.backend.* stores without weakening auth."""
    candidates = [
        "admin_console.backend.auth",
        "auth",
        "admin_console.backend.business",
        "business",
        "admin_console.backend.infra",
        "infra",
        "admin_console.backend.managed",
        "managed",
        "admin_console.backend.user_mappings",
        "user_mappings",
        "admin_console.backend.llm_providers",
        "llm_providers",
        "admin_console.backend.runtime_mode",
        "runtime_mode",
    ]
    # Use set to avoid double-clearing same object twice (bare + canonical alias may be same)
    seen_ids = set()
    for mod_name in candidates:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        oid = id(mod)
        if oid in seen_ids:
            continue
        seen_ids.add(oid)
        for fn in ("clear_users", "clear_services", "clear_license", "clear_backups",
                    "clear_upgrade", "clear_tickets", "clear_mappings",
                    "clear_providers", "clear_quotas", "clear_usage"):
            try:
                attr = getattr(mod, fn, None)
                if callable(attr):
                    attr()
            except Exception:
                pass
        # clear dict stores except _license_state (structured reset via clear_license above)
        for dict_name in ("_quota_store", "_quota_window_counts", "_providers",
                          "_encrypted_store", "_secret_refs", "_mappings",
                          "_backup_history", "_fernet_cache",
                          "_admin_usage_records"):
            try:
                d = getattr(mod, dict_name, None)
                if isinstance(d, dict):
                    d.clear()
                elif isinstance(d, list):
                    try:
                        d.clear()
                    except Exception:
                        pass
            except Exception:
                pass
        # deque for usage
        try:
            dq = getattr(mod, "_admin_usage_records", None)
            if dq is not None and hasattr(dq, "clear") and not isinstance(dq, dict):
                try:
                    dq.clear()
                except Exception:
                    pass
        except Exception:
            pass

    # also clear any admin_* isolated copies (admin_auth_biz etc.)
    for name, mod in list(sys.modules.items()):
        if name.startswith("admin_auth") or name.startswith("admin_llm") or name.startswith("admin_business") or name.startswith("admin_user_mappings") or name.startswith("admin_infra") or name.startswith("admin_managed") or name.startswith("admin_app"):
            if id(mod) in seen_ids:
                continue
            for fn in ("clear_users", "clear_services", "clear_license", "clear_backups",
                        "clear_upgrade", "clear_tickets", "clear_mappings",
                        "clear_providers", "clear_quotas", "clear_usage"):
                try:
                    attr = getattr(mod, fn, None)
                    if callable(attr):
                        attr()
                except Exception:
                    pass
            for dict_name in ("_quota_store", "_quota_window_counts", "_providers", "_encrypted_store", "_secret_refs"):
                try:
                    d = getattr(mod, dict_name, None)
                    if isinstance(d, dict):
                        d.clear()
                except Exception:
                    pass
            # ensure isolated llm mod syncs to canonical after clear (so later inspection sees canonical state)
            if name.startswith("admin_llm"):
                try:
                    canon = _canonical_llm_module()
                    if canon and canon is not mod:
                        for dict_name in ("_providers", "_quota_store", "_quota_window_counts", "_encrypted_store", "_secret_refs", "_admin_usage_records"):
                            try:
                                cd = getattr(canon, dict_name, None)
                                pd = getattr(mod, dict_name, None)
                                if isinstance(cd, dict) and isinstance(pd, dict):
                                    # alias private dict to canonical's dict object for inspection consistency
                                    # We don't replace dict object to keep reference stability; just ensure both empty
                                    pd.clear()
                                elif hasattr(cd, "clear") and hasattr(pd, "clear"):
                                    pd.clear()
                            except Exception:
                                pass
                except Exception:
                    pass

    # reset DB engine caches so OAOS_DATABASE_URL changes are picked up
    for mod_name in ("admin_console.backend.llm_providers", "llm_providers",
                     "admin_console.backend.runtime_mode", "runtime_mode",
                     "admin_console.backend.persistence", "persistence",
                     "admin_console.backend.business", "business"):
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for attr in ("_db_engine", "_db_session_factory", "_db_cached_url"):
            try:
                if hasattr(mod, attr):
                    setattr(mod, attr, None)
            except Exception:
                pass
        try:
            fc = getattr(mod, "_fernet_cache", None)
            if isinstance(fc, dict):
                fc.clear()
        except Exception:
            pass
        # clear any on-disk sqlite test file handle
        try:
            eng = getattr(mod, "_db_engine", None)
            if eng is not None:
                try:
                    eng.dispose()
                except Exception:
                    pass
        except Exception:
            pass

    # clean up temp sqlite test file if any test left it
    try:
        Path("/tmp/test_llm_provider_vault.db").unlink(missing_ok=True)
    except Exception:
        pass

    # reset llm_runtime in-memory quota/usage
    try:
        from agent_runtime.llm_runtime import _llm_quota_clear, clear_llm_usage
        _llm_quota_clear()
        clear_llm_usage()
    except Exception:
        pass

    # ensure runtime_mode canonical is hermes after each test (tests that need llm set it explicitly in their own fixture)
    # Always reset to hermes; individual llm tests will set llm in their fixture after this before-clean.
    for mod_name in ("admin_console.backend.runtime_mode", "runtime_mode"):
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        # delete persisted DB value
        eng = None
        try:
            eng = mod._get_engine() if hasattr(mod, "_get_engine") else None
        except Exception:
            eng = None
        if eng is not None:
            try:
                from sqlalchemy import text
                with eng.begin() as conn:
                    conn.execute(text("DELETE FROM admin_settings WHERE key='runtime_mode'"))
            except Exception:
                pass
            try:
                eng.dispose()
            except Exception:
                pass
            mod._db_engine = None
            mod._db_session_factory = None
        try:
            if hasattr(mod, "_current_mode") and hasattr(mod, "RuntimeMode"):
                mod._current_mode = mod.RuntimeMode.hermes
        except Exception:
            pass
    # env runtime mode reset to hermes unless snapshot will restore llm (but we always want hermes default)
    # We leave env handling to caller; but ensure in-memory is hermes
    os.environ.pop("OAOS_RUNTIME_MODE", None)

@pytest.fixture(autouse=True)
def _global_admin_isolation():
    snap_before = _snapshot_env()
    os.environ.setdefault("OAOS_VAULT_KEY", "test-vault-key-for-llm-provider-32bytes!!")
    _clear_canonical_admin_state()
    yield
    _clear_canonical_admin_state()
    _restore_env(snap_before)
    # force hermes after restore even if snapshot had llm (snapshot llm only if previous test needed it, but we want default hermes)
    # If snapshot had llm, keep it only if test explicitly needs it next; but default is hermes, so pop unless snapshot says llm and next test will set itself.
    # Actually restore already did; now ensure _current_mode hermes if env not llm
    for mod_name in ("admin_console.backend.runtime_mode", "runtime_mode"):
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        try:
            if hasattr(mod, "_current_mode") and hasattr(mod, "RuntimeMode"):
                env_mode = os.environ.get("OAOS_RUNTIME_MODE", "").lower()
                if env_mode != "llm":
                    mod._current_mode = mod.RuntimeMode.hermes
                else:
                    mod._current_mode = mod.RuntimeMode.llm
        except Exception:
            pass
        # clear DB engine again post-restore
        try:
            mod._db_engine = None
            mod._db_session_factory = None
        except Exception:
            pass
    for _k in (
        "OAOS_SIGNING_KEY",
        "OAOS_SECURITY_SERVICE_SIGNING_KEY",
        "OAOS_USER_JWT_SIGNING_KEY",
        "OAOS_JWT_SIGNING_KEY",
        "OAOS_AGENT_CONTEXT_SIGNING_KEY",
        "OAOS_AGENT_JWT_SIGNING_KEY",
        "OAOS_SIGNED_CONTEXT_SIGNING_KEY",
        "JWT_SIGNING_KEY",
        "ADMIN_JWT_SECRET",
        "OAOS_WIKI_JWT_SIGNING_KEY",
    ):
        os.environ[_k] = UNIFIED_TEST_KEY
    os.environ.pop("OAOS_ENV", None)
    if not os.environ.get("OAOS_VAULT_KEY") and not os.environ.get("VAULT_ENCRYPTION_KEY"):
        os.environ["OAOS_VAULT_KEY"] = "test-vault-key-for-llm-provider-32bytes!!"

@pytest.fixture
def tenant_id(): return "test-tenant"
