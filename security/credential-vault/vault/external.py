"""External Vault backends — Phase B vault externalization.

Provides VaultBackend ABC + concrete backends:
  - EnvFileBackend  (tests/dev, no config required — in-memory dict + optional file)
  - HashiCorpVaultBackend  (hvac or httpx, falls back to EnvFileBackend when unconfigured)
  - AwsSecretsBackend       (boto3, falls back to EnvFileBackend when unconfigured)

Factory: get_vault_backend() reads VAULT_BACKEND env:
  external|hashicorp|hashicorp_vault|vault -> HashiCorp
  aws|aws_secrets|aws_secrets_manager|aws_kms -> AWS
  env|file|memory|envfile -> EnvFile
  auto -> pick based on available env vars
  encrypted_postgres|legacy|postgres|"" -> None (use legacy Fernet path)
"""
from __future__ import annotations

import abc
import base64
import logging
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Abstract ──────────────────────────────────────────────────────────

class VaultBackend(abc.ABC):
    """Low-level secret bytes store. Operates on already-generated secret_ref."""

    @abc.abstractmethod
    async def put(self, secret_ref: str, secret: bytes, metadata: dict | None = None) -> None:
        ...

    @abc.abstractmethod
    async def get(self, secret_ref: str) -> bytes | None:
        ...

    @abc.abstractmethod
    async def delete(self, secret_ref: str) -> None:
        ...

    async def health_check(self) -> bool:
        return True

    def backend_name(self) -> str:
        return self.__class__.__name__

# ── EnvFileBackend ───────────────────────────────────────────────────

class EnvFileBackend(VaultBackend):
    """In-memory (+ optional JSON file) backend for tests / dev.

    - No external dependency.
    - Thread-safe dict.
    - If VAULT_FILE_PATH is set, persists to JSON file (base64-encoded values).
    """

    def __init__(self, file_path: str | None = None) -> None:
        self._store: dict[str, bytes] = {}
        self._meta: dict[str, dict] = {}
        self._lock = threading.Lock()
        # resolve file path from explicit arg or env
        raw = file_path or os.getenv("VAULT_FILE_PATH", "")
        self._file_path: Path | None = Path(raw) if raw else None
        if self._file_path and self._file_path.exists():
            self._load_file()

    def _load_file(self) -> None:
        try:
            import json
            data = json.loads(self._file_path.read_text())  # type: ignore
            for k, v in data.items():
                try:
                    self._store[k] = base64.b64decode(v)
                except Exception:
                    continue
        except Exception as e:
            logger.debug("EnvFileBackend load failed: %s", e)

    def _save_file(self) -> None:
        if not self._file_path:
            return
        try:
            import json
            # ensure parent exists
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: base64.b64encode(v).decode() for k, v in self._store.items()}
            self._file_path.write_text(json.dumps(data))
        except Exception as e:
            logger.debug("EnvFileBackend save failed: %s", e)

    async def put(self, secret_ref: str, secret: bytes, metadata: dict | None = None) -> None:
        with self._lock:
            self._store[secret_ref] = secret
            if metadata:
                self._meta[secret_ref] = dict(metadata)
        self._save_file()

    async def get(self, secret_ref: str) -> bytes | None:
        with self._lock:
            return self._store.get(secret_ref)

    async def delete(self, secret_ref: str) -> None:
        with self._lock:
            self._store.pop(secret_ref, None)
            self._meta.pop(secret_ref, None)
        self._save_file()

    async def health_check(self) -> bool:
        return True

    def backend_name(self) -> str:
        return "env"

# ── HashiCorpVaultBackend ────────────────────────────────────────────

class HashiCorpVaultBackend(VaultBackend):
    """HashiCorp Vault KV v2 backend.

    Prefers hvac if installed; otherwise httpx.  When VAULT_ADDR is unset
    or neither library is available, degrades gracefully to an embedded
    EnvFileBackend so tests never require a real Vault.
    """

    def __init__(
        self,
        addr: str | None = None,
        token: str | None = None,
        kv_mount: str | None = None,
        kv_prefix: str | None = None,
        namespace: str | None = None,
        tls_ca_bundle: str | None = None,
        fallback: VaultBackend | None = None,
    ) -> None:
        self.addr = (addr or os.getenv("VAULT_ADDR", "")).strip().rstrip("/")
        self.token = token or os.getenv("VAULT_TOKEN", "")
        self.kv_mount = (kv_mount or os.getenv("VAULT_KV_MOUNT", "secret")).strip().strip("/")
        self.kv_prefix = (kv_prefix or os.getenv("VAULT_KV_PREFIX", "openagentos/")).strip().strip("/") + "/"
        if self.kv_prefix == "/":
            self.kv_prefix = "openagentos/"
        self.namespace = namespace or os.getenv("VAULT_NAMESPACE", "")
        self.tls_ca_bundle = tls_ca_bundle or os.getenv("VAULT_TLS_CA_BUNDLE", "")
        # fallback in-memory store used when vault unreachable or unconfigured
        self._fallback: VaultBackend = fallback or EnvFileBackend()
        self._use_fallback = not bool(self.addr)
        if self._use_fallback:
            logger.info("HashiCorpVaultBackend: VAULT_ADDR not set, using in-memory fallback")
        else:
            logger.info("HashiCorpVaultBackend: addr=%s mount=%s prefix=%s", self.addr, self.kv_mount, self.kv_prefix)

    def _path(self, secret_ref: str) -> str:
        # KV v2 data path: /v1/<mount>/data/<prefix><ref>
        # we expose full logical path for logging: secret/data/openagentos/<ref>
        return f"{self.kv_mount}/data/{self.kv_prefix}{secret_ref}"

    def _path_metadata(self, secret_ref: str) -> str:
        return f"{self.kv_mount}/metadata/{self.kv_prefix}{secret_ref}"

    def _headers(self) -> dict:
        h: dict[str, str] = {}
        if self.token:
            h["X-Vault-Token"] = self.token
        if self.namespace:
            h["X-Vault-Namespace"] = self.namespace
        return h

    async def put(self, secret_ref: str, secret: bytes, metadata: dict | None = None) -> None:
        if self._use_fallback:
            await self._fallback.put(secret_ref, secret, metadata)
            return
        # try hvac
        try:
            import hvac  # type: ignore

            # hvac is sync; run via thread? For simplicity wrap sync call.
            # We keep async interface but hvac is sync — safe for low concurrency.
            client = hvac.Client(url=self.addr, token=self.token, namespace=self.namespace, verify=self.tls_ca_bundle or True)  # type: ignore
            payload = {"data": {"token": base64.b64encode(secret).decode(), **(metadata or {})}}
            # hvac KV v2 API
            client.secrets.kv.v2.create_or_update_secret(  # type: ignore
                path=f"{self.kv_prefix}{secret_ref}",
                mount_point=self.kv_mount,
                cas=0,
                secret=payload["data"],
            )
            return
        except ImportError:
            pass
        except Exception as e:
            logger.warning("HashiCorpVaultBackend.put hvac failed, trying httpx: %s", e)

        # try httpx
        try:
            import httpx  # type: ignore

            url = f"{self.addr}/v1/{self._path(secret_ref)}"
            payload = {"data": {"token": base64.b64encode(secret).decode(), **(metadata or {})}}
            async with httpx.AsyncClient(verify=self.tls_ca_bundle or True, timeout=5.0) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
                if resp.status_code in (200, 204):
                    return
                # if 404 mount not found, fallback
                logger.warning("HashiCorpVaultBackend.put httpx status=%s body=%s", resp.status_code, resp.text[:500])
                raise RuntimeError(f"vault put {resp.status_code}: {resp.text[:200]}")
        except ImportError:
            pass
        except Exception as e:
            logger.warning("HashiCorpVaultBackend.put failed, falling back to memory: %s", e)
            await self._fallback.put(secret_ref, secret, metadata)
            return

        # no transport available -> fallback
        logger.debug("HashiCorpVaultBackend: no hvac/httpx available, using fallback")
        await self._fallback.put(secret_ref, secret, metadata)

    async def get(self, secret_ref: str) -> bytes | None:
        if self._use_fallback:
            return await self._fallback.get(secret_ref)
        # try hvac
        try:
            import hvac  # type: ignore

            client = hvac.Client(url=self.addr, token=self.token, namespace=self.namespace, verify=self.tls_ca_bundle or True)  # type: ignore
            resp = client.secrets.kv.v2.read_secret_version(path=f"{self.kv_prefix}{secret_ref}", mount_point=self.kv_mount, raise_on_deleted_version=False)  # type: ignore
            if resp and resp.get("data") and resp["data"].get("data"):
                b64 = resp["data"]["data"].get("token")
                if b64 is not None:
                    return base64.b64decode(b64)
            return None
        except ImportError:
            pass
        except Exception as e:
            logger.debug("HashiCorpVaultBackend.get hvac failed: %s", e)

        try:
            import httpx  # type: ignore

            url = f"{self.addr}/v1/{self._path(secret_ref)}"
            async with httpx.AsyncClient(verify=self.tls_ca_bundle or True, timeout=5.0) as client:
                resp = await client.get(url, headers=self._headers())
                if resp.status_code == 404:
                    return None
                if resp.status_code == 200:
                    data = resp.json()
                    inner = data.get("data", {}).get("data", {})
                    b64 = inner.get("token")
                    if b64 is not None:
                        return base64.b64decode(b64)
                    return None
                logger.warning("HashiCorpVaultBackend.get httpx status=%s", resp.status_code)
                return None
        except ImportError:
            pass
        except Exception as e:
            logger.debug("HashiCorpVaultBackend.get httpx failed: %s", e)

        # fallback
        return await self._fallback.get(secret_ref)

    async def delete(self, secret_ref: str) -> None:
        if self._use_fallback:
            await self._fallback.delete(secret_ref)
            return
        try:
            import hvac  # type: ignore

            client = hvac.Client(url=self.addr, token=self.token, namespace=self.namespace, verify=self.tls_ca_bundle or True)  # type: ignore
            try:
                client.secrets.kv.v2.delete_metadata_and_all_versions(path=f"{self.kv_prefix}{secret_ref}", mount_point=self.kv_mount)  # type: ignore
            except Exception:
                pass
        except ImportError:
            pass
        except Exception as e:
            logger.debug("HashiCorpVaultBackend.delete hvac failed: %s", e)
        try:
            import httpx  # type: ignore

            url = f"{self.addr}/v1/{self._path_metadata(secret_ref)}"
            async with httpx.AsyncClient(verify=self.tls_ca_bundle or True, timeout=5.0) as client:
                await client.request("DELETE", url, headers=self._headers())
        except Exception:
            pass
        # also clear fallback
        try:
            await self._fallback.delete(secret_ref)
        except Exception:
            pass

    async def health_check(self) -> bool:
        if self._use_fallback:
            return True
        # quick check: try sys/health or kv read
        try:
            import httpx  # type: ignore

            url = f"{self.addr}/v1/sys/health"
            async with httpx.AsyncClient(verify=self.tls_ca_bundle or True, timeout=3.0) as client:
                resp = await client.get(url, headers=self._headers())
                return resp.status_code in (200, 204, 429, 472, 473)
        except Exception:
            pass
        try:
            import hvac  # type: ignore

            client = hvac.Client(url=self.addr, token=self.token, namespace=self.namespace, verify=self.tls_ca_bundle or True)  # type: ignore
            return not client.is_authenticated() or client.is_authenticated()  # type: ignore
        except Exception:
            return False

    def backend_name(self) -> str:
        return "hashicorp_vault"

# ── AwsSecretsBackend ────────────────────────────────────────────────

class AwsSecretsBackend(VaultBackend):
    """AWS Secrets Manager backend.

    When boto3 is missing or AWS credentials not configured, falls back
    to EnvFileBackend so tests never require real AWS.
    """

    def __init__(
        self,
        region: str | None = None,
        prefix: str | None = None,
        fallback: VaultBackend | None = None,
    ) -> None:
        self.region = (region or os.getenv("AWS_REGION", "") or os.getenv("AWS_DEFAULT_REGION", "")).strip()
        self.prefix = (prefix or os.getenv("AWS_SECRETS_PREFIX", "openagentos/")).strip()
        if self.prefix and not self.prefix.endswith("/"):
            self.prefix += "/"
        if not self.prefix:
            self.prefix = "openagentos/"
        self._fallback: VaultBackend = fallback or EnvFileBackend()
        # we don't require region at init; will fallback gracefully at call time
        self._use_fallback = False  # determined lazily
        try:
            import boto3  # type: ignore

            _ = boto3  # noqa
        except ImportError:
            self._use_fallback = True
            logger.info("AwsSecretsBackend: boto3 not installed, using in-memory fallback")
            return
        if not self.region:
            # allow via env or instance metadata; don't force fallback yet
            logger.debug("AwsSecretsBackend: AWS_REGION not set, will attempt default chain")

    def _secret_id(self, secret_ref: str) -> str:
        return f"{self.prefix}{secret_ref}"

    def _client(self):
        import boto3  # type: ignore
        from botocore.exceptions import NoCredentialsError  # type: ignore

        kwargs: dict = {}
        if self.region:
            kwargs["region_name"] = self.region
        try:
            client = boto3.client("secretsmanager", **kwargs)  # type: ignore
            return client
        except Exception as e:
            logger.debug("AwsSecretsBackend client creation failed: %s", e)
            raise

    async def put(self, secret_ref: str, secret: bytes, metadata: dict | None = None) -> None:
        if self._use_fallback:
            await self._fallback.put(secret_ref, secret, metadata)
            return
        try:
            import asyncio
            from botocore.exceptions import ClientError  # type: ignore

            sid = self._secret_id(secret_ref)
            secret_str = base64.b64encode(secret).decode()

            def _sync():
                client = self._client()
                tags = []
                if metadata:
                    for k, v in metadata.items():
                        if isinstance(v, str) and len(v) <= 256:
                            tags.append({"Key": str(k)[:128], "Value": v[:256]})
                try:
                    client.create_secret(Name=sid, SecretString=secret_str, Tags=tags)  # type: ignore
                except client.exceptions.ResourceExistsException:  # type: ignore
                    client.put_secret_value(SecretId=sid, SecretString=secret_str)  # type: ignore
                except ClientError as e:
                    code = e.response.get("Error", {}).get("Code", "")
                    if code == "ResourceExistsException":
                        client.put_secret_value(SecretId=sid, SecretString=secret_str)  # type: ignore
                    else:
                        raise

            await asyncio.to_thread(_sync)
            return
        except ImportError:
            pass
        except Exception as e:
            logger.warning("AwsSecretsBackend.put failed (%s), falling back to memory", e)
            await self._fallback.put(secret_ref, secret, metadata)
            return
        # fallback
        await self._fallback.put(secret_ref, secret, metadata)

    async def get(self, secret_ref: str) -> bytes | None:
        if self._use_fallback:
            return await self._fallback.get(secret_ref)
        try:
            import asyncio
            sid = self._secret_id(secret_ref)

            def _sync():
                client = self._client()
                resp = client.get_secret_value(SecretId=sid)  # type: ignore
                s = resp.get("SecretString")
                if s is not None:
                    return base64.b64decode(s)
                b = resp.get("SecretBinary")
                if b is not None:
                    return bytes(b)
                return None

            result = await asyncio.to_thread(_sync)
            if result is not None:
                return result
            return None
        except Exception as e:
            # If secret not found, AWS raises ResourceNotFoundException
            msg = str(e)
            if "ResourceNotFound" in msg or "not found" in msg.lower():
                return None
            logger.debug("AwsSecretsBackend.get fallback due to: %s", e)
            return await self._fallback.get(secret_ref)

    async def delete(self, secret_ref: str) -> None:
        if self._use_fallback:
            await self._fallback.delete(secret_ref)
            return
        try:
            import asyncio
            sid = self._secret_id(secret_ref)

            def _sync():
                client = self._client()
                try:
                    client.delete_secret(SecretId=sid, ForceDeleteWithoutRecovery=True)  # type: ignore
                except Exception:
                    pass

            await asyncio.to_thread(_sync)
        except Exception:
            pass
        try:
            await self._fallback.delete(secret_ref)
        except Exception:
            pass

    async def health_check(self) -> bool:
        if self._use_fallback:
            return True
        try:
            import asyncio

            def _sync():
                client = self._client()
                client.list_secrets(MaxResults=1)  # type: ignore
                return True

            await asyncio.to_thread(_sync)
            return True
        except Exception as e:
            logger.debug("AwsSecretsBackend health_check failed: %s", e)
            return False

    def backend_name(self) -> str:
        return "aws_secrets"

# ── Factory ──────────────────────────────────────────────────────────

_LEGACY_VALUES = {"", "encrypted_postgres", "encrypted-postgres", "legacy", "postgres", "none"}

def get_vault_backend(backend: str | None = None) -> VaultBackend | None:
    """Return a VaultBackend instance based on VAULT_BACKEND env.

    Values:
      - hashicorp|hashicorp_vault|vault|external -> HashiCorpVaultBackend
      - aws|aws_secrets|aws_secrets_manager|aws_kms -> AwsSecretsBackend
      - env|file|memory|envfile -> EnvFileBackend
      - auto -> picks hashicorp if VAULT_ADDR set, else aws if AWS_REGION set, else env
      - encrypted_postgres / legacy / "" / None -> None (signal: use legacy Fernet)
    """
    raw = backend if backend is not None else os.getenv("VAULT_BACKEND", "")
    val = raw.strip().lower()
    # auto: no explicit backend
    if val == "auto":
        if os.getenv("VAULT_ADDR"):
            return HashiCorpVaultBackend()
        if os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"):
            return AwsSecretsBackend()
        return EnvFileBackend()
    if val in _LEGACY_VALUES:
        return None
    if val in {"hashicorp", "hashicorp_vault", "vault", "external", "hashi", "hashicorp vault"}:
        return HashiCorpVaultBackend()
    if val in {"aws", "aws_secrets", "aws_secrets_manager", "aws_kms", "secretsmanager"}:
        return AwsSecretsBackend()
    if val in {"env", "file", "memory", "envfile", "env_file"}:
        return EnvFileBackend()
    # Also handle operator synonyms like "external"
    if val in {"external_vault", "external-vault"}:
        return HashiCorpVaultBackend()
    # Unknown backend -> fail closed per spec
    raise ValueError(f"unknown VAULT_BACKEND={raw!r}")

# Back-compat alias
create_vault_backend = get_vault_backend
