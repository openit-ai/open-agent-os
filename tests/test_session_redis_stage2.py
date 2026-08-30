"""Stage 2 TDD — Redis primary in production (H5 fail-closed).

Requirements:
1) production global session_store must be RedisSessionStore fallback=False, fail-closed if Redis unavailable
2) session namespace/model fields persisted and survive Redis reload/restart (serialization)
3) non-prod compatibility: memory or redis with fallback allowed
"""
import os
import sys
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in [ROOT / "control-plane", ROOT / "security" / "policy-engine", ROOT / "packages" / "policy-model"]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest

def _fakeredis_client():
    try:
        import fakeredis
        return fakeredis.FakeRedis(decode_responses=True)
    except Exception as e:
        pytest.skip(f"fakeredis not available: {e}")

def _reload_session_module(env):
    """Reload control_plane.session with given env dict, return module."""
    old_env = {k: os.environ.get(k) for k in env}
    # set new
    for k,v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    # also need to clear other related
    # force reload
    if "control_plane.session" in sys.modules:
        del sys.modules["control_plane.session"]
    # also clear env_gate cache? just reload
    import control_plane.session as m
    import importlib; importlib.reload(m)
    # restore caller env after? we capture old to restore in test teardown
    return m, old_env

def _restore_env(old_env):
    for k,v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    # reload again to avoid leakage
    if "control_plane.session" in sys.modules:
        del sys.modules["control_plane.session"]
        import control_plane.session as m
        importlib.reload(m)

# --- Test 1: production global uses Redis ---
def test_production_global_session_store_is_redis():
    """In production global session_store must be RedisSessionStore fallback=False."""
    # ensure redis reachable via fakeredis monkeypatch? we need to make RedisSessionStore succeed without real redis
    # Patch redis.Redis.from_url to return fakeredis so production init succeeds
    r = _fakeredis_client()
    import redis
    orig = redis.Redis.from_url
    redis.Redis.from_url = lambda *a, **kw: r
    env = {"OAOS_ENV": "production", "OAOS_SESSION_BACKEND": "redis", "REDIS_URL": "redis://localhost:6379/0"}
    mod = None
    old = {}
    try:
        mod, old = _reload_session_module(env)
        from control_plane.session import session_store, RedisSessionStore
        # need to reload after patch? already did
        import control_plane.session as sess_mod
        assert isinstance(sess_mod.session_store, RedisSessionStore), f"expected RedisSessionStore in production, got {type(sess_mod.session_store)}"
        assert sess_mod.session_store._fallback_store is None, "production must have fallback=False (no fallback_store)"
        assert sess_mod.session_store._client is not None
    finally:
        redis.Redis.from_url = orig
        _restore_env(old)
        # also pop test env
        for k in ["OAOS_ENV","OAOS_SESSION_BACKEND","REDIS_URL"]:
            if k not in old or old[k] is None:
                os.environ.pop(k, None)

def test_production_global_requires_redis_even_without_backend_env():
    """Production without OAOS_SESSION_BACKEND should still use Redis (fail-closed)."""
    r = _fakeredis_client()
    import redis
    orig = redis.Redis.from_url
    redis.Redis.from_url = lambda *a, **kw: r
    env = {"OAOS_ENV": "production", "REDIS_URL": "redis://localhost:6379/0"}
    # ensure backend not set
    env["OAOS_SESSION_BACKEND"] = None
    old = {}
    try:
        mod, old = _reload_session_module(env)
        import control_plane.session as sess_mod
        from control_plane.session import RedisSessionStore
        assert isinstance(sess_mod.session_store, RedisSessionStore), f"production without backend env must still be Redis, got {type(sess_mod.session_store)}"
        assert sess_mod.session_store._fallback_store is None
    finally:
        redis.Redis.from_url = orig
        _restore_env(old)

# --- Test 2: production no fallback, startup must fail if redis unavailable ---
def test_production_no_fallback_startup_fails_when_redis_unavailable():
    """If Redis unavailable in production, import/initialization must raise (fail-closed), not fallback."""
    import redis
    orig = redis.Redis.from_url
    def _broken(*a, **kw):
        raise ConnectionError("redis down simulated")
    redis.Redis.from_url = _broken
    env = {"OAOS_ENV": "production", "OAOS_SESSION_BACKEND": "redis", "REDIS_URL": "redis://127.0.0.1:59999/0"}
    old = {}
    try:
        # reloading should raise
        old_env_snapshot = {k: os.environ.get(k) for k in env}
        for k,v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if "control_plane.session" in sys.modules:
            del sys.modules["control_plane.session"]
        with pytest.raises(RuntimeError) as ei:
            import control_plane.session  # noqa
        assert "Redis unavailable" in str(ei.value) or "fallback" in str(ei.value).lower()
    finally:
        redis.Redis.from_url = orig
        for k in env:
            os.environ.pop(k, None)
        for k,v in old_env_snapshot.items():
            if v is not None:
                os.environ[k] = v
        if "control_plane.session" in sys.modules:
            del sys.modules["control_plane.session"]
            import importlib
            import control_plane.session as m
            importlib.reload(m)

def test_production_redis_store_fallback_true_rejected():
    """Explicit fallback=True in production must be rejected."""
    env = {"OAOS_ENV": "production"}
    old = {k: os.environ.get(k) for k in env}
    for k,v in env.items():
        os.environ[k] = v
    try:
        from control_plane.session import RedisSessionStore
        with pytest.raises(RuntimeError) as ei:
            RedisSessionStore(redis_url="redis://127.0.0.1:59999/0", fallback=True)
        assert "fallback not allowed" in str(ei.value).lower()
    finally:
        for k,v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

# --- Test 3: serialization persistence namespace/model ---
def test_session_namespace_model_persisted_across_reload():
    """namespace and model fields must be persisted and survive Redis reload (new store instance same client)."""
    r = _fakeredis_client()
    from control_plane.session import RedisSessionStore
    store1 = RedisSessionStore(redis_client=r, ttl_seconds=60, key_prefix="test:oaos:stage2:")
    # ensure clean
    try: r.flushdb()
    except: pass
    rec = store1.create(tenant_id="t1", user_id="u1", agent_id="a1", security_domain="general")
    # mutate namespace/model to non-default to verify persistence
    rec.session_namespace = "oaos:mattermost"
    rec.runtime_model = "muse-spark-1.2-contributor"
    rec.runtime_provider = "opencode-go"
    # save via append to force persist
    store1._save(rec)
    # also test to_dict/from_dict includes those fields
    d = rec.to_dict()
    assert d["session_namespace"] == "oaos:mattermost"
    assert d["runtime_model"] == "muse-spark-1.2-contributor"
    assert d["runtime_provider"] == "opencode-go"
    # new store instance same redis client (simulates service restart with same redis)
    store2 = RedisSessionStore(redis_client=r, ttl_seconds=60, key_prefix="test:oaos:stage2:")
    got = store2.get(rec.session_id, caller_user_id="u1")
    assert got.session_namespace == "oaos:mattermost"
    assert got.runtime_model == "muse-spark-1.2-contributor"
    assert got.runtime_provider == "opencode-go"
    # also prompt_history survives
    store1.append_prompt(rec.session_id, caller_user_id="u1", prompt="hello world", request_id="req-1")
    got2 = store2.get(rec.session_id, caller_user_id="u1")
    assert len(got2.prompt_history) == 1
    assert got2.prompt_history[0]["prompt"] == "hello world"

# --- Test 4: non-prod compatibility ---
def test_non_prod_compatibility_memory_or_fallback():
    """Non-prod should allow InMemory or Redis with fallback."""
    # ensure non-prod env
    old_env = {k: os.environ.get(k) for k in ["OAOS_ENV","OAOS_SESSION_BACKEND","OAOS_ALLOW_TEST_FALLBACK"]}
    for k in ["OAOS_ENV","OAOS_SESSION_BACKEND"]:
        os.environ.pop(k, None)
    # reload to get InMemory
    if "control_plane.session" in sys.modules:
        del sys.modules["control_plane.session"]
    import importlib, control_plane.session as m
    importlib.reload(m)
    from control_plane.session import InMemorySessionStore
    assert isinstance(m.session_store, InMemorySessionStore), f"non-prod without backend should be InMemory, got {type(m.session_store)}"
    # non-prod Redis with fallback should work even when redis down (fallback)
    os.environ.pop("OAOS_ENV", None)
    from control_plane.session import RedisSessionStore
    store = RedisSessionStore(redis_url="redis://127.0.0.1:59999/0", fallback=True)
    assert store._fallback_store is not None
    rec = store.create(tenant_id="t1", user_id="u1", agent_id="a1")
    assert store.get(rec.session_id, caller_user_id="u1").session_id == rec.session_id
    # restore
    for k,v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    if "control_plane.session" in sys.modules:
        del sys.modules["control_plane.session"]
        import control_plane.session as m2
        importlib.reload(m2)
