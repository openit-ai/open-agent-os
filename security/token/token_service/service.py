"""Capability Token — Section 26. Short-lived signed token, nonce + replay protection.
- HS256 JWT
- nonce store for replay 방지
- short-lived 300s (default)
- verify with expiry + nonce + signature
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import jwt, JWTError, ExpiredSignatureError

ALG = "HS256"
DEFAULT_TTL_SECONDS = 300


class TokenService:
    """Stateful token service with in-memory nonce/jti replay store."""

    def __init__(self, signing_key: str, default_ttl: int = DEFAULT_TTL_SECONDS) -> None:
        self.signing_key = signing_key
        self.default_ttl = default_ttl
        # jti/nonce → issued_at (replay 방지)
        self._seen_nonces: set[str] = set()
        self._seen_jtis: set[str] = set()
        # revoked jtis
        self._revoked: set[str] = set()

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
    ) -> str:
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        now = datetime.now(timezone.utc)
        payload = {
            "sub": sub,
            "on_behalf_of": on_behalf_of,
            "action": action,
            "resource": resource,
            "session_id": session_id,
            "request_id": request_id,
            "delegation_id": delegation_id,
            "nonce": uuid.uuid4().hex,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl)).timestamp()),
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(payload, self.signing_key, algorithm=ALG)

    def verify(self, token: str) -> dict:
        """검증: signature + expiry + nonce replay + revoked check.

        Raises:
            ValueError on replay / revoked / invalid
            JWTError / ExpiredSignatureError on signature/expiry fail
        """
        try:
            payload = jwt.decode(token, self.signing_key, algorithms=[ALG])
        except ExpiredSignatureError:
            raise
        except JWTError as e:
            raise ValueError(f"invalid token: {e}") from e

        jti = payload.get("jti")
        nonce = payload.get("nonce")

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
                self._revoked.add(jti)
        except JWTError:
            pass

    def is_revoked(self, jti: str) -> bool:
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
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "on_behalf_of": on_behalf_of,
        "action": action,
        "resource": resource,
        "session_id": session_id,
        "request_id": request_id,
        "delegation_id": delegation_id,
        "nonce": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
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
    """
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
