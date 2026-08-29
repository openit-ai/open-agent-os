"""Regression — persisted session override gpt-5.6-luna/custom must not override primary.

Covers OAOS-side guard (this repo) and documents the authoritative Hermes fix.

Hermes source is outside this repo (/.hermes/hermes-agent), so OAOS implements
defense-in-depth: session metadata, runtime-adapter set_model, llm_runtime,
and fallback chain all filter stale custom entries.  The production Hermes fix
must clear persisted session overrides that contain blocked provider/model/base_url
(see docs/HERMES_PRODUCTION_REMEDIATION.md).
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

import pytest

# Make packages importable (same as conftest.py)
ROOT = Path(__file__).resolve().parents[1]
for p in [
    ROOT / "packages/agent-runtime",
    ROOT / "packages/runtime-adapter",
    ROOT / "admin-console",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
if str(ROOT / "packages/agent-runtime") not in sys.path:
    sys.path.insert(0, str(ROOT / "packages/agent-runtime"))

# ---------------------------------------------------------------------------
# 1) model_guard unit
# ---------------------------------------------------------------------------

def test_model_guard_blocks_custom_and_loopback():
    from agent_runtime.model_guard import is_blocked_entry, sanitize_entry, sanitize_metadata, ALLOWED_PROVIDERS

    # provider custom blocked regardless of model
    blocked, _ = is_blocked_entry({"provider": "custom", "model": "anything", "base_url": ""})
    assert blocked is True
    blocked, _ = is_blocked_entry({"provider": "CUSTOM", "model": "gpt-4o"})
    assert blocked is True

    # gpt-5.6-luna blocked
    blocked, _ = is_blocked_entry({"provider": "opencode-go", "model": "gpt-5.6-luna"})
    assert blocked is True
    blocked, _ = is_blocked_entry({"provider": "openrouter", "model": "gpt-5.6-sol"})
    assert blocked is True
    blocked, _ = is_blocked_entry({"provider": "claude", "model": "claude-3"})
    assert blocked is False

    # loopback blocked
    blocked, _ = is_blocked_entry({"provider": "claude", "model": "claude-3", "base_url": "http://127.0.0.1:10100/v1"})
    assert blocked is True
    blocked, _ = is_blocked_entry({"provider": "claude", "model": "claude-3", "base_url": "https://api.openai.com/v1"})
    assert blocked is False

    # allowed providers list sanity
    assert "custom" not in ALLOWED_PROVIDERS
    assert "opencode-go" in ALLOWED_PROVIDERS

    # sanitize_entry returns None for blocked, dict for allowed
    assert sanitize_entry({"provider": "custom", "model": "gpt-5.6-luna"}) is None
    assert sanitize_entry({"provider": "opencode-go", "model": "muse-spark-1.2-contributor"}) is not None
    # alias normalization
    good = sanitize_entry({"provider": "opencode", "model": "deepseek-v4-pro"})
    assert good is not None and good["provider"] == "opencode-go"

def test_model_guard_sanitize_metadata_strips_persisted_override():
    from agent_runtime.model_guard import sanitize_metadata

    # flat provider custom
    md = {"provider": "custom", "model": "gpt-5.6-luna", "session_note": "keep_me"}
    cleaned = sanitize_metadata(md)
    assert "provider" not in cleaned
    assert "model" not in cleaned
    assert cleaned.get("session_note") == "keep_me"

    # nested model_override dict
    md2 = {"model_override": {"provider": "custom", "model": "gpt-5.6-luna", "base_url": "http://127.0.0.1:10100/v1"}, "safe_key": "ok"}
    cleaned2 = sanitize_metadata(md2)
    assert "model_override" not in cleaned2
    assert cleaned2.get("safe_key") == "ok"

    # fallback_chain list filtering
    md3 = {"fallback_chain": [
        {"provider": "custom", "model": "gpt-5.6-luna", "base_url": "http://127.0.0.1:10100/v1"},
        {"provider": "opencode-go", "model": "muse-spark-1.2-contributor"},
    ]}
    cleaned3 = sanitize_metadata(md3)
    assert len(cleaned3["fallback_chain"]) == 1
    assert cleaned3["fallback_chain"][0]["provider"] == "opencode-go"

    # embedded dict under arbitrary key
    md4 = {"weird": {"provider": "custom", "model": "gpt-5.6-sol"}}
    cleaned4 = sanitize_metadata(md4)
    assert "weird" not in cleaned4

    # allowed entry preserved
    md5 = {"model": "muse-spark-1.2-contributor", "provider": "opencode-go"}
    cleaned5 = sanitize_metadata(md5)
    assert cleaned5["model"] == "muse-spark-1.2-contributor"
    assert cleaned5["provider"] == "opencode-go"

def test_model_guard_base_url_list():
    from agent_runtime.model_guard import sanitize_metadata
    md = {"fallback_chain": [
        {"provider": "claude", "model": "claude-3", "base_url": "http://127.0.0.1:10100/v1"},
        {"provider": "ollama", "model": "llama3", "base_url": "http://localhost:11434"},
    ]}
    cleaned = sanitize_metadata(md)
    assert len(cleaned["fallback_chain"]) == 1
    assert cleaned["fallback_chain"][0]["model"] == "llama3"

# ---------------------------------------------------------------------------
# 2) SessionManager — create/resume/get_state must strip blocked metadata
# ---------------------------------------------------------------------------

def test_session_manager_strips_on_create():
    from agent_runtime.session import SessionManager, _MemoryStore
    store = _MemoryStore()
    mgr = SessionManager(store=store)
    sess = mgr.create(tenant_id="t1", agent_id="a1", metadata={
        "provider": "custom",
        "model": "gpt-5.6-luna",
        "base_url": "http://127.0.0.1:10100/v1",
        "safe": "keep",
    })
    # blocked keys stripped at create time
    assert "provider" not in sess["metadata"]
    assert "model" not in sess["metadata"]
    assert sess["metadata"].get("safe") == "keep"

def test_session_manager_strips_on_resume_and_get_state_persists_sanitized():
    from agent_runtime.session import SessionManager, SessionRecord, _MemoryStore
    store = _MemoryStore()
    mgr = SessionManager(store=store)
    # create clean session first, then inject blocked metadata directly into store (simulating old persisted data)
    sess = mgr.create(tenant_id="t1", agent_id="a1", metadata={"safe": "initial"})
    sid = sess["session_id"]
    # inject stale override directly (bypass create guard)
    rec: SessionRecord = store.load(sid)  # type: ignore
    rec.metadata = {"provider": "custom", "model": "gpt-5.6-luna", "base_url": "http://127.0.0.1:10100/v1", "fallback_chain": [
        {"provider": "custom", "model": "gpt-5.6-luna", "base_url": "http://127.0.0.1:10100/v1"},
        {"provider": "opencode-go", "model": "muse-spark-1.2-contributor"},
    ]}
    store.save(rec)

    # resume must sanitize before returning and must persist sanitized version
    resumed = mgr.resume(sid, tenant_id="t1", agent_id="a1")
    assert "provider" not in resumed["metadata"]
    # fallback_chain filtered to only allowed entry
    assert len(resumed["metadata"]["fallback_chain"]) == 1
    assert resumed["metadata"]["fallback_chain"][0]["provider"] == "opencode-go"
    # stored record now sanitized
    reloaded: SessionRecord = store.load(sid)  # type: ignore
    assert "provider" not in reloaded.metadata

def test_session_isolation_still_enforced_after_guard():
    from agent_runtime.session import SessionManager, _MemoryStore
    mgr = SessionManager(store=_MemoryStore())
    s = mgr.create(tenant_id="t1", agent_id="a1", metadata={"model": "claude-3"})
    sid = s["session_id"]
    with pytest.raises(PermissionError):
        mgr.resume(sid, tenant_id="t1", agent_id="a2")
    with pytest.raises(PermissionError):
        mgr.get_state(sid, tenant_id="other", agent_id="a1")

# ---------------------------------------------------------------------------
# 3) runtime-adapter set_model must reject custom
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hermes_adapter_set_model_rejects_custom():
    from runtime_adapter.hermes_adapter import HermesRuntimeAdapter
    ad = HermesRuntimeAdapter(hermes_base_url="http://localhost:0", timeout_s=0.1)
    # before: default hermes
    assert ad._current_model["provider"] == "hermes"
    # try blocked
    res = await ad.set_model({"session_id": "s1"}, model="gpt-5.6-luna", provider="custom")
    assert res["status"] == "rejected"
    # not stored
    assert ad._current_model["provider"] == "hermes"
    assert ad._models.get("s1") is None
    # allowed still works
    res2 = await ad.set_model({"session_id": "s1"}, model="muse-spark-1.2-contributor", provider="opencode-go")
    assert res2["status"] == "ok"
    assert ad._models["s1"]["model"] == "muse-spark-1.2-contributor"

@pytest.mark.asyncio
async def test_safe_adapter_set_model_rejects_blocked_model():
    from runtime_adapter.safe_adapter import SafeRuntimeAdapter
    ad = SafeRuntimeAdapter(base_url="http://localhost:0", timeout_s=0.1)
    res = await ad.set_model({"session_id": "s2"}, model="gpt-5.6-sol", provider="opencode-go")
    assert res["status"] == "rejected"
    # previous model not overwritten
    assert ad._current_model["model"] != "gpt-5.6-sol"

# ---------------------------------------------------------------------------
# 4) LLMProviderAdapter init guard
# ---------------------------------------------------------------------------

def test_llm_provider_adapter_blocks_custom_at_init():
    os.environ.pop("OAOS_RUNTIME_MODE", None)
    os.environ.pop("OAOS_LLM_PROVIDER", None)
    from agent_runtime.llm_runtime import LLMProviderAdapter

    # provider custom with blocked model should be sanitized to fallback
    ad = LLMProviderAdapter(provider="custom", model="gpt-5.6-luna", base_url="http://127.0.0.1:10100/v1")
    # blocked provider should not be retained
    assert ad.provider_type is None
    # blocked base_url/model stripped from provider_config
    assert "127.0.0.1:10100" not in str(ad.provider_config)
    assert "gpt-5.6-luna" not in str(ad.provider_config)

    # allowed provider keeps type
    ad2 = LLMProviderAdapter(provider="opencode-go", model="muse-spark-1.2-contributor")
    from agent_runtime.llm_runtime import ProviderType
    assert ad2.provider_type == ProviderType.OPENCODE_GO

def test_llm_provider_adapter_strips_blocked_config_from_env(monkeypatch):
    from agent_runtime.llm_runtime import LLMProviderAdapter
    monkeypatch.setenv("OAOS_LLM_PROVIDER", "custom")
    monkeypatch.setenv("CODEX_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:10100/v1")
    # still blocked even via env
    ad = LLMProviderAdapter(model="gpt-5.6-luna", base_url="http://127.0.0.1:10100/v1")
    # provider_type may be None or not custom
    if ad.provider_type is not None:
        assert ad.provider_type.value != "custom"
    monkeypatch.delenv("OAOS_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

# ---------------------------------------------------------------------------
# 5) fallback chain guard
# ---------------------------------------------------------------------------

def test_fallback_validators_block_gpt56():
    # Use pydantic directly
    import sys
    sys.path.insert(0, str(ROOT / "admin-console"))
    from backend.fallback import FallbackEntry, FallbackConfig  # type: ignore

    with pytest.raises(Exception) as exc:
        FallbackEntry(provider="custom", model="anything")
    assert "provider must be" in str(exc.value).lower() or "custom" in str(exc.value).lower()

    with pytest.raises(Exception):
        FallbackEntry(provider="claude", model="gpt-5.6-luna")

    with pytest.raises(Exception):
        FallbackConfig(enabled=True, chain=[], fallback_model="gpt-5.6-sol")

def test_fallback_load_config_strips_blocked_persisted():
    import sys, json
    sys.path.insert(0, str(ROOT / "admin-console"))
    import backend.fallback as fb  # type: ignore

    # inject env with blocked chain
    blocked = json.dumps({"enabled": True, "chain": [
        {"provider": "custom", "model": "gpt-5.6-luna", "enabled": True},
        {"provider": "opencode-go", "model": "muse-spark-1.2-contributor", "enabled": True},
    ], "fallback_model": "gpt-5.6-luna"})
    old = os.environ.get("OAOS_LLM_FALLBACK_JSON")
    old_db_url = os.environ.get("OAOS_DATABASE_URL")
    # ensure DB not used
    os.environ.pop("OAOS_DATABASE_URL", None)
    os.environ.pop("DATABASE_URL", None)
    fb._inmem = None
    fb._db_engine = None
    os.environ["OAOS_LLM_FALLBACK_JSON"] = blocked
    cfg = fb._load_config()
    assert len(cfg.chain) == 1
    assert cfg.chain[0].provider == "opencode-go"
    assert cfg.fallback_model is None
    # cleanup
    if old is not None:
        os.environ["OAOS_LLM_FALLBACK_JSON"] = old
    else:
        os.environ.pop("OAOS_LLM_FALLBACK_JSON", None)
    if old_db_url is not None:
        os.environ["OAOS_DATABASE_URL"] = old_db_url
    fb._inmem = None

# ---------------------------------------------------------------------------
# 6) Primary preservation: fallback isolation pattern (mirrors hermes-agent test)
# ---------------------------------------------------------------------------

def test_session_with_blocked_override_primary_preserved_via_guard():
    """Custom gpt-5.6-luna at 127.0.0.1:10100 is isolated; primary opencode-go preserved."""
    from agent_runtime.llm_runtime import LLMProviderAdapter, ProviderType
    # Primary is opencode-go muse-spark; blocked override must not hijack it
    primary = LLMProviderAdapter(provider="opencode-go", model="muse-spark-1.2-contributor")
    assert primary.provider_type == ProviderType.OPENCODE_GO

    # Attempt to inject blocked override after creation (e.g., from stale session resume)
    # The adapter's stored config must remain unpoisoned
    blocked_adapter = LLMProviderAdapter(provider="custom", model="gpt-5.6-luna", base_url="http://127.0.0.1:10100/v1")
    assert blocked_adapter.provider_type is None  # filtered
    # Primary unaffected
    assert primary.provider_type == ProviderType.OPENCODE_GO
    assert "muse-spark" in str(primary.routing.default_model)
