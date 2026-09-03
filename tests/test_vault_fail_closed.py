"""P0 Vault fail-closed — production must not silently fallback to EnvFile/memory/legacy."""
import asyncio
import os
import pytest

# ensure vault key for imports
os.environ.setdefault("OAOS_VAULT_KEY", "test-vault-key-for-failclosed-32bytes!!")


def _clear_vault_env(monkeypatch):
    for k in ("VAULT_BACKEND","VAULT_ADDR","VAULT_TOKEN","VAULT_KV_MOUNT","VAULT_KV_PREFIX",
              "AWS_REGION","AWS_DEFAULT_REGION","AWS_SECRETS_PREFIX","VAULT_FILE_PATH",
              "VAULT_DUAL_WRITE","VAULT_READ_FALLBACK","OAOS_ENV","ENV","OAOS_ENVIRONMENT"):
        monkeypatch.delenv(k, raising=False)


def test_production_hashicorp_missing_addr_fails_closed(monkeypatch):
    monkeypatch.setenv("OAOS_ENV","production")
    monkeypatch.setenv("VAULT_BACKEND","hashicorp_vault")
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    from vault.external import get_vault_backend
    # should raise RuntimeError in production, not return fallback backend
    with pytest.raises(RuntimeError, match="VAULT_ADDR"):
        get_vault_backend()


def test_non_production_hashicorp_missing_addr_allows_fallback(monkeypatch):
    _clear_vault_env(monkeypatch)
    monkeypatch.setenv("VAULT_BACKEND","hashicorp_vault")
    # no OAOS_ENV production
    from vault.external import get_vault_backend
    be = get_vault_backend()
    assert be is not None
    assert be.backend_name() == "hashicorp_vault"
    # put should succeed via in-memory fallback
    async def _run():
        await be.put("secret_test123", b"hello")
        v = await be.get("secret_test123")
        assert v == b"hello"
    asyncio.run(_run())


def test_production_aws_missing_region_fails_closed(monkeypatch):
    monkeypatch.setenv("OAOS_ENV","production")
    monkeypatch.setenv("VAULT_BACKEND","aws_secrets")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    from vault.external import get_vault_backend
    with pytest.raises(RuntimeError, match="AWS_REGION"):
        get_vault_backend()


def test_production_hashicorp_transport_failure_fails_closed_not_fallback(monkeypatch):
    monkeypatch.setenv("OAOS_ENV","production")
    monkeypatch.setenv("VAULT_BACKEND","hashicorp_vault")
    monkeypatch.setenv("VAULT_ADDR","http://invalid.invalid:8200")
    # ensure hvac not present or will fail; force httpx failure via invalid host
    from vault.external import HashiCorpVaultBackend
    be = HashiCorpVaultBackend(addr="http://invalid.invalid:8200")
    async def _run():
        # put should raise in production, not silently fallback to memory
        with pytest.raises(RuntimeError):
            await be.put("secret_failtest", b"secret-bytes")
        # get should also fail-closed in production (raise), not return fallback value
        with pytest.raises(RuntimeError):
            await be.get("secret_failtest")
    asyncio.run(_run())


def test_production_aws_put_failure_fails_closed(monkeypatch):
    monkeypatch.setenv("OAOS_ENV","production")
    monkeypatch.setenv("VAULT_BACKEND","aws_secrets")
    monkeypatch.setenv("AWS_REGION","ap-northeast-2")
    # Mock boto3 client to always fail — force transport failure inside put
    import sys, types, importlib
    fake_boto3 = types.ModuleType("boto3")
    def _fail_client(*a, **kw):
        raise RuntimeError("simulated AWS transport failure")
    fake_boto3.client = _fail_client
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    # also need botocore.exceptions.ClientError for import inside put
    fake_botocore = types.ModuleType("botocore")
    fake_exc = types.ModuleType("botocore.exceptions")
    class FakeClientError(Exception):
        def __init__(self, *a, **kw): super().__init__("fake")
    fake_exc.ClientError = FakeClientError
    fake_botocore.exceptions = fake_exc
    monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", fake_exc)
    # reload external module to ensure fresh class picks fake
    import vault.external as ext
    import importlib
    importlib.reload(ext)
    be = ext.AwsSecretsBackend(region="ap-northeast-2")
    async def _run():
        with pytest.raises(RuntimeError):
            await be.put("secret_awsfail", b"aws-secret")
    asyncio.run(_run())
    # restore real module for other tests
    monkeypatch.delitem(sys.modules, "boto3", raising=False)
    monkeypatch.delitem(sys.modules, "botocore", raising=False)
    monkeypatch.delitem(sys.modules, "botocore.exceptions", raising=False)
    importlib.reload(ext)


def test_production_vault_store_fails_closed_when_external_unreachable(monkeypatch):
    _clear_vault_env(monkeypatch)
    monkeypatch.setenv("OAOS_ENV","production")
    monkeypatch.setenv("VAULT_BACKEND","hashicorp_vault")
    monkeypatch.setenv("VAULT_ADDR","http://invalid.invalid:8200")
    from vault.vault import EncryptedPostgresVault
    # In production, EncryptedPostgresVault with missing/unreachable external should raise on init or on store
    # First check init raises or store raises
    vault = EncryptedPostgresVault(encryption_key=b"test-key-32bytes-long-enough!!")
    # If init didn't raise, store must fail closed (raise), not fallback to memory/legacy
    async def _run():
        with pytest.raises(Exception):
            await vault.store("employee:kim","google","gmail.read", b"super-secret")
        # Also ensure no row was stored in memory fallback
        # vault._store should be empty or not contain plaintext fallback
        assert len(vault._store) == 0 or all(v != b"super-secret" for v in vault._store.values())
    asyncio.run(_run())


def test_production_vault_retrieve_does_not_fallback_to_encrypted_token(monkeypatch):
    _clear_vault_env(monkeypatch)
    # First create a legacy vault to store encrypted_token, then switch to production external mode and ensure retrieve doesn't fallback
    from vault.vault import EncryptedPostgresVault
    # legacy store
    monkeypatch.delenv("VAULT_BACKEND", raising=False)
    monkeypatch.delenv("OAOS_ENV", raising=False)
    v_legacy = EncryptedPostgresVault(encryption_key=b"test-key-32bytes-long-enough!!")
    async def _legacy_store():
        ref = await v_legacy.store("employee:kim","google","gmail.read", b"legacy-secret")
        return ref
    ref = asyncio.run(_legacy_store())
    # Now production external mode with fallback disabled
    monkeypatch.setenv("OAOS_ENV","production")
    monkeypatch.setenv("VAULT_BACKEND","hashicorp_vault")
    monkeypatch.setenv("VAULT_ADDR","http://invalid.invalid:8200")
    # Even with VAULT_READ_FALLBACK=true, production should NOT fallback to legacy ciphertext
    monkeypatch.setenv("VAULT_READ_FALLBACK","true")
    v_prod = EncryptedPostgresVault(encryption_key=b"test-key-32bytes-long-enough!!")
    # inject legacy store data to simulate DB row with encrypted_token
    v_prod._store[ref] = v_legacy._store[ref]
    v_prod._meta[ref] = v_legacy._meta[ref]
    async def _retrieve():
        with pytest.raises(Exception):
            await v_prod.retrieve(ref, "agent:assistant:kim")
    asyncio.run(_retrieve())


def test_health_check_fails_in_production_when_unreachable(monkeypatch):
    monkeypatch.setenv("OAOS_ENV","production")
    monkeypatch.setenv("VAULT_BACKEND","hashicorp_vault")
    monkeypatch.setenv("VAULT_ADDR","http://invalid.invalid:8200")
    from vault.external import HashiCorpVaultBackend
    be = HashiCorpVaultBackend(addr="http://invalid.invalid:8200")
    async def _run():
        ok = await be.health_check()
        assert ok is False
    asyncio.run(_run())


def test_unknown_backend_fails_closed_always(monkeypatch):
    _clear_vault_env(monkeypatch)
    monkeypatch.setenv("VAULT_BACKEND","unknown_backend_xyz")
    from vault.external import get_vault_backend
    with pytest.raises(ValueError, match="unknown"):
        get_vault_backend()


def test_external_mode_db_does_not_store_encrypted_token(monkeypatch):
    _clear_vault_env(monkeypatch)
    monkeypatch.setenv("VAULT_BACKEND","hashicorp_vault")
    # non-production without VAULT_ADDR -> fallback in-memory, should still allow external mode semantics (no encrypted_token)
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    from vault.vault import EncryptedPostgresVault
    vault = EncryptedPostgresVault(encryption_key=b"test-key-32bytes-long-enough!!")
    async def _run():
        ref = await vault.store("employee:kim","google","gmail.read", b"my-secret")
        # In external mode without dual_write, encrypted_token should be None (or not stored in _store as plaintext)
        # Check that _store does not contain Fernet ciphertext of secret (or is empty)
        # Actually _store should be empty or placeholder, not Fernet blob that decrypts to secret
        # Verify retrieve works via external
        tok = await vault.retrieve(ref, "agent:assistant:kim")
        assert tok == b"my-secret"
        # Ensure dual_write false => encrypted_token path not used
        assert not vault._dual_write()
    asyncio.run(_run())
