"""Capability Token — Section 26. Short-lived signed token, nonce + replay protection.
- HS256 JWT
- nonce store for replay 방지
- short-lived 300s (default)
- verify with expiry + nonce + signature

Distributed/operational boundary:
- Production (OAOS_ENV=production): Redis is primary for replay/revoke state (distributed).
  In-memory fallback is allowed ONLY in non-prod (explicit test fallback, OAOS_ALLOW_TEST_FALLBACK).
  Limitations: in-memory is process-local, not shared across replicas and does not survive
  restart — prod MUST set REDIS_URL and use Redis backend. When REDIS_URL is unavailable in
  prod, verify/issue fail-closed.
"""
from __future__ import annotations

import hashlib
import uuid
import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import jwt, JWTError, ExpiredSignatureError

logger = logging.getLogger(__name__)

ALG = "HS256"
DEFAULT_TTL_SECONDS = 300

# ── env helpers ──────────────────────────────────────────────────

def _is_prod() -> bool:
    return os.environ.get("OAOS_ENV", "").lower() in ("production", "prod")


def _redis_url() -> str | None:
    u = os.environ.get("REDIS_URL") or os.environ.get("OAOS_REDIS_URL")
    return u.strip() if u and u.strip() else None


def _allow_in_memory_fallback() -> bool:
    if _is_prod():
        return os.environ.get("OAOS_ALLOW_TEST_FALLBACK", "").lower() in ("1", "true", "yes")
    return True


def _require_redis_if_prod() -> None:
    if _is_prod() and not _redis_url():
        raise RuntimeError("TokenService: REDIS_URL required when OAOS_ENV=production (fail-closed — distributed replay store must be Redis-backed)")


def _redis_client():
    url = _redis_url()
    if not url:
        return None
    # Lazy import — fail-closed in prod if unavailable
    try:
        import redis  # type: ignore

        return redis.Redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
    except Exception as e:
        if _is_prod():
            raise RuntimeError(f"TokenService: redis client unavailable in production: {e}") from e
        logger.debug("TokenService redis unavailable (non-prod fallback): %s", e)
        return None


def _redis_key(kind: str, value: str) -> str:
    # Namespace replay keys per deployment
    return f"oaos:token:{kind}:{value}"


class TokenService:
    """Stateful token service with in-memory nonce/jti replay store + optional Redis primary.

    Production: Redis is primary (distributed). In-memory fallback ONLY in non-prod.
    See module docstring for limitations.
    """

    def __init__(self, signing_key: str, default_ttl: int = DEFAULT_TTL_SECONDS) -> None:
        self.signing_key = signing_key
        self.default_ttl = default_ttl
        # jti/nonce → issued_at (replay 방지) — in-memory fallback for non-prod/tests
        self._seen_nonces: set[str] = set()
        self._seen_jtis: set[str] = set()
        # revoked jtis
        self._revoked: set[str] = set()
        if _is_prod():
            _require_redis_if_prod()

    def issue(
        self,
        sub: str,
        on_behalf_of: str,
        action: str,
        resource: str,
        session_id: str,
        request_id: str,
        delegation_id: str | None = None,
        ttl_seconds: int | None = None,
        tenant_id: str | None = None,
        **kwargs,
    ) -> str:
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        # backward compat: allow tenant/tid alias
        if tenant_id is None:
            tenant_id = kwargs.get("tenant_id") or kwargs.get("tenant") or kwargs.get("tid")
        now = datetime.now(timezone.utc)
        payload = {
            "sub": sub,
            "on_behalf_of": on_behalf_of,
            "action": action,
            "resource": resource,
            "session_id": session_id,
            "request_id": request_id,
            "delegation_id": delegation_id,
            "tenant_id": tenant_id,
            "tenant": tenant_id,
            "tid": tenant_id,
            "nonce": uuid.uuid4().hex,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl)).timestamp()),
            "jti": uuid.uuid4().hex,
        }
        # remove None tenant aliases to keep backward compat with older verifiers that expect missing=skip
        if tenant_id is None:
            payload.pop("tenant_id", None)
            payload.pop("tenant", None)
            payload.pop("tid", None)
        return jwt.encode(payload, self.signing_key, algorithm=ALG)

    def verify(self, token: str) -> dict:
        """검증: signature + expiry + nonce replay + revoked check.

        Raises:
            ValueError on replay / revoked / invalid
            JWTError / ExpiredSignatureError on signature/expiry fail
            RuntimeError on missing Redis in production (fail-closed)
        """
        try:
            payload = jwt.decode(token, self.signing_key, algorithms=[ALG])
        except ExpiredSignatureError:
            raise
        except JWTError as e:
            raise ValueError(f"invalid token: {e}") from e

        jti = payload.get("jti")
        nonce = payload.get("nonce")
        exp_ts = payload.get("exp")
        # TTL for redis keys: time until expiry + small buffer, default 600s if missing
        try:
            ttl = int(exp_ts) - int(datetime.now(timezone.utc).timestamp()) + 60 if exp_ts else 600
            if ttl < 60:
                ttl = 60
        except Exception:
            ttl = 600

        # Redis primary path (distributed) — try first if REDIS_URL set
        r = _redis_client()
        if r is not None:
            try:
                # revoked check
                if jti and r.exists(_redis_key("revoked", str(jti))):
                    raise ValueError("token revoked")
                # replay check via SET NX
                if jti:
                    ok = r.set(_redis_key("jti", str(jti)), "1", nx=True, ex=ttl)
                    if not ok:
                        raise ValueError("token replay detected")
                if nonce:
                    ok = r.set(_redis_key("nonce", str(nonce)), "1", nx=True, ex=ttl)
                    if not ok:
                        # rollback jti key to avoid half-state? keep it as replay anyway
                        raise ValueError("token replay detected")
                # also mirror in-memory for local fast path
                if jti:
                    self._seen_jtis.add(str(jti))
                if nonce:
                    self._seen_nonces.add(str(nonce))
                return payload
            except ValueError:
                raise
            except Exception as e:
                if _is_prod():
                    raise RuntimeError(f"TokenService verify failed — Redis unavailable in production: {e}") from e
                logger.debug("TokenService Redis verify fallback to memory: %s", e)
                # fall through to in-memory for non-prod

        # No Redis or non-prod fallback
        if _is_prod():
            raise RuntimeError("TokenService verify failed — Redis required in production but unavailable (fail-closed)")

        if jti in self._revoked:
            raise ValueError("token revoked")

        # replay 방지: 동일 jti/nonce 재사용 거부
        if jti in self._seen_jtis or (nonce and nonce in self._seen_nonces):
            raise ValueError("token replay detected")

        # 통과하면 기록
        if jti:
            self._seen_jtis.add(jti)
        if nonce:
            self._seen_nonces.add(nonce)

        return payload

    def verify_without_replay_check(self, token: str) -> dict:
        """replay 체크 없이 signature+expiry 만 검증 (내부용)."""
        return jwt.decode(token, self.signing_key, algorithms=[ALG])

    def revoke(self, token: str) -> None:
        """jti 기반 revoke — 즉시 무효화."""
        try:
            payload = jwt.decode(
                token, self.signing_key, algorithms=[ALG], options={"verify_exp": False}
            )
            jti = payload.get("jti")
            if jti:
                # Redis primary if available
                r = _redis_client()
                if r is not None:
                    try:
                        exp_ts = payload.get("exp")
                        ttl = int(exp_ts) - int(datetime.now(timezone.utc).timestamp()) + 60 if exp_ts else 600
                        if ttl < 60:
                            ttl = 60
                        r.set(_redis_key("revoked", str(jti)), "1", ex=ttl)
                        # also record in-memory
                        self._revoked.add(str(jti))
                        return
                    except Exception as e:
                        if _is_prod():
                            raise RuntimeError(f"TokenService revoke failed — Redis unavailable in production: {e}") from e
                        logger.debug("TokenService Redis revoke fallback to memory: %s", e)
                if _is_prod() and _redis_url():
                    raise RuntimeError("TokenService revoke failed — Redis required in production but unavailable (fail-closed)")
                self._revoked.add(str(jti))
        except RuntimeError:
            raise
        except JWTError:
            pass

    def is_revoked(self, jti: str) -> bool:
        # Redis primary check if available
        r = _redis_client()
        if r is not None:
            try:
                if r.exists(_redis_key("revoked", str(jti))):
                    return True
            except Exception as e:
                if _is_prod():
                    raise RuntimeError(f"TokenService is_revoked Redis unavailable in production: {e}") from e
        return jti in self._revoked


# ── Stateless helper 함수 (기존 인터페이스 호환) ───────────────

_global_nonce_store: set[str] = set()
_global_jti_store: set[str] = set()


def issue_capability_token(
    signing_key: str,
    sub: str,
    on_behalf_of: str,
    action: str,
    resource: str,
    session_id: str,
    request_id: str,
    delegation_id: str | None = None,
    ttl_seconds: int = 300,
    tenant_id: str | None = None,
    **kwargs,
) -> str:
    if tenant_id is None:
        tenant_id = kwargs.get("tenant_id") or kwargs.get("tenant") or kwargs.get("tid")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "on_behalf_of": on_behalf_of,
        "action": action,
        "resource": resource,
        "session_id": session_id,
        "request_id": request_id,
        "delegation_id": delegation_id,
        "tenant_id": tenant_id,
        "tenant": tenant_id,
        "tid": tenant_id,
        "nonce": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if tenant_id is None:
        payload.pop("tenant_id", None)
        payload.pop("tenant", None)
        payload.pop("tid", None)
    return jwt.encode(payload, signing_key, algorithm=ALG)


def verify_capability_token(
    signing_key: str,
    token: str,
    nonce_store: set[str] | None = None,
    jti_store: set[str] | None = None,
) -> dict:
    """Stateless verify with optional external nonce/jti store for replay 방지.

    - nonce_store / jti_store 를 전달하면 replay 감지 후 기록
    - 전달하지 않으면 전역 store 사용
    - If REDIS_URL is set, Redis (distributed) is primary with same semantics.
    - Production fail-closed: when OAOS_ENV=production, Redis is required if configured;
      missing/unreachable Redis raises RuntimeError.
    """
    # Redis primary path if configured
    r = _redis_client()
    if r is not None:
        payload = jwt.decode(token, signing_key, algorithms=[ALG])
        jti = payload.get("jti")
        nonce = payload.get("nonce")
        exp_ts = payload.get("exp")
        try:
            ttl = int(exp_ts) - int(datetime.now(timezone.utc).timestamp()) + 60 if exp_ts else 600
            if ttl < 60:
                ttl = 60
        except Exception:
            ttl = 600
        try:
            if jti:
                ok = r.set(_redis_key("jti", str(jti)), "1", nx=True, ex=ttl)
                if not ok:
                    raise ValueError("token replay detected (jti)")
            if nonce:
                ok = r.set(_redis_key("nonce", str(nonce)), "1", nx=True, ex=ttl)
                if not ok:
                    raise ValueError("token replay detected (nonce)")
            # mirror in-memory for non-prod speed
            if jti and jti_store is not None:
                jti_store.add(str(jti))
            elif jti:
                _global_jti_store.add(str(jti))
            if nonce and nonce_store is not None:
                nonce_store.add(str(nonce))
            elif nonce:
                _global_nonce_store.add(str(nonce))
            return payload
        except ValueError:
            raise
        except Exception as e:
            if _is_prod():
                raise RuntimeError(f"verify_capability_token Redis unavailable in production: {e}") from e
            # fall through to in-memory for non-prod
    if _is_prod() and _redis_url():
        raise RuntimeError("verify_capability_token Redis required in production but unavailable (fail-closed)")
    # Explicit test fallback only non-prod: in-memory
    if _is_prod() and not _allow_in_memory_fallback():
        raise RuntimeError("verify_capability_token in-memory fallback not allowed in production (fail-closed)")

    ns = nonce_store if nonce_store is not None else _global_nonce_store
    js = jti_store if jti_store is not None else _global_jti_store

    payload = jwt.decode(token, signing_key, algorithms=[ALG])
    jti = payload.get("jti")
    nonce = payload.get("nonce")

    if jti and jti in js:
        raise ValueError("token replay detected (jti)")
    if nonce and nonce in ns:
        raise ValueError("token replay detected (nonce)")

    if jti:
        js.add(jti)
    if nonce:
        ns.add(nonce)

    return payload


def clear_global_stores() -> None:
    """테스트용 전역 store 초기화."""
    _global_nonce_store.clear()
    _global_jti_store.clear()
