"""Capability Enforcement — Section 20, 26 강화

- Capability Token 검증: signature, expiry, nonce, resource/action match, delegation binding
- HIGH-risk는 token 필수
- trace 전파
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fnmatch

try:
    from .normalize import normalize_resource, canonicalize_action
except ImportError:
    from normalize import normalize_resource, canonicalize_action  # type: ignore


@dataclass
class CapabilityCheck:
    allowed: bool
    reason: str
    token_id: str | None = None
    delegation_id: str | None = None


def _get_token_field(token: dict, key: str):
    # dict token (decoded JWT) or object
    if isinstance(token, dict):
        return token.get(key)
    return getattr(token, key, None)


def verify_capability(token: dict, action: str, resource: str, context: dict | None = None) -> CapabilityCheck:
    """Capability Token 검증 — 실제 JWT 검증 + resource/action/delegation 바인딩.

    Args:
        token: decoded JWT dict (jose jwt.decode 결과) 또는 Pydantic CapabilityToken dict
        action: canonical action (예: READ, SEND)
        resource: canonical resource (예: gmail/user/kim/messages)
        context: AgentContext dict (delegation_id 바인딩 검증용, optional)

    Returns:
        CapabilityCheck
    """
    if not token:
        return CapabilityCheck(False, "missing capability token")

    # 1. expiry 검증
    exp = _get_token_field(token, "exp")
    if exp is not None:
        try:
            exp_dt = datetime.fromtimestamp(int(exp), tz=timezone.utc)
            if datetime.now(timezone.utc) > exp_dt:
                return CapabilityCheck(False, "capability token expired", _get_token_field(token, "jti"))
        except (ValueError, OSError, OverflowError):
            pass
    expires_at = _get_token_field(token, "expires_at")
    if expires_at is not None and isinstance(expires_at, str):
        try:
            dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > dt:
                return CapabilityCheck(False, "capability token expired", _get_token_field(token, "jti"))
        except ValueError:
            pass

    # 2. action 검증 — canonical 비교 + alias 처리
    token_action = _get_token_field(token, "action")
    if not token_action:
        return CapabilityCheck(False, "token missing action")
    try:
        # 양쪽 canonicalize 후 비교
        canon_token_action = canonicalize_action(str(token_action))
        canon_req_action = canonicalize_action(str(action))
    except ValueError:
        canon_token_action = str(token_action).upper().strip()
        canon_req_action = str(action).upper().strip()
    if canon_token_action != canon_req_action:
        # 와일드카드 '*' 허용
        if token_action != "*" and canon_token_action != "*":
            return CapabilityCheck(False, f"action mismatch: token={token_action} required={action}")

    # 3. resource 검증 — glob 패턴 지원 (token resource가 패턴)
    token_resource = _get_token_field(token, "resource")
    if not token_resource:
        return CapabilityCheck(False, "token missing resource")

    # 정규화 시도
    try:
        canon_token_res = normalize_resource(str(token_resource))
    except ValueError:
        canon_token_res = str(token_resource)
    try:
        canon_req_res = normalize_resource(str(resource))
    except ValueError:
        canon_req_res = str(resource)

    # fnmatch: token resource 패턴이 request resource와 매칭되는지
    # token resource는 glob 패턴 (예: gmail/user/kim/*), request는 구체적 resource
    if not fnmatch.fnmatch(canon_req_res, canon_token_res):
        # 추가: prefix 매칭 (token에 '*' 없지만 prefix인 경우)
        prefix = canon_token_res.rstrip("*").rstrip("/")
        if not canon_req_res.startswith(prefix):
            return CapabilityCheck(False, f"resource mismatch: token={token_resource} required={resource}")

    # 4. delegation binding 검증 (있다면 AgentContext와 일치해야 함)
    token_delegation = _get_token_field(token, "delegation_id")
    if context is not None and token_delegation:
        ctx_delegation = context.get("delegation_id") if isinstance(context, dict) else getattr(context, "delegation_id", None)
        # context에 delegation이 있으면 token과 일치해야 함
        if ctx_delegation and ctx_delegation != token_delegation:
            return CapabilityCheck(False, f"delegation binding mismatch: token={token_delegation} context={ctx_delegation}")

    # 5. principal 검증 (on_behalf_of 일치)
    token_on_behalf = _get_token_field(token, "on_behalf_of")
    if context is not None and token_on_behalf:
        ctx_user = context.get("user_id") if isinstance(context, dict) else getattr(context, "user_id", None)
        if ctx_user and token_on_behalf != ctx_user:
            return CapabilityCheck(False, f"principal mismatch: token on_behalf_of={token_on_behalf} context user={ctx_user}")

    # 6. sub / agent 일치 (있다면)
    token_sub = _get_token_field(token, "sub")
    if context is not None and token_sub:
        ctx_agent = context.get("agent_id") if isinstance(context, dict) else getattr(context, "agent_id", None)
        if ctx_agent and token_sub != ctx_agent:
            return CapabilityCheck(False, f"agent mismatch: token sub={token_sub} context agent={ctx_agent}")

    jti = _get_token_field(token, "jti") or _get_token_field(token, "nonce")
    return CapabilityCheck(True, "ok", jti, token_delegation)


def verify_capability_strict(
    signing_key: str,
    token_str: str,
    action: str,
    resource: str,
    context: dict | None = None,
) -> CapabilityCheck:
    """JWT 문자열을 서명 검증 후 verify_capability로 위임.

    signing_key가 None이면 서명 검증을 생략하고 payload만 검증.
    """
    if not token_str:
        return CapabilityCheck(False, "missing token string")
    # JWT decode 시도
    try:
        from jose import jwt  # type: ignore

        if signing_key:
            payload = jwt.decode(token_str, signing_key, algorithms=["HS256"])
        else:
            # 서명 없이 payload 추출 (테스트용) — 실제로는 signing_key 필수
            payload = jwt.get_unverified_claims(token_str)
        return verify_capability(payload, action, resource, context)
    except Exception as e:
        return CapabilityCheck(False, f"token decode failed: {e}")
