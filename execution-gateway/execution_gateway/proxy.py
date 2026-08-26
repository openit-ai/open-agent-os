"""Privileged Tool Proxy — Section 7.2 강화

- capability token 검증 + delegation binding + trace 전파
- HIGH-risk는 token 필수
- resource/action normalization
- risk 분류 + audit trace 유지
"""
from __future__ import annotations

import uuid
from typing import Any
from datetime import datetime, timezone

try:
    from .capability import verify_capability
    from .risk import classify, RiskLevel
    from .normalize import normalize_resource, canonicalize_action
except ImportError:
    from execution_gateway.capability import verify_capability  # type: ignore
    from execution_gateway.risk import classify, RiskLevel  # type: ignore
    from execution_gateway.normalize import normalize_resource, canonicalize_action  # type: ignore


async def proxy_tool_call(
    tool_name: str,
    args: dict,
    capability_token: dict | str | None,
    context: dict,
) -> dict:
    """Tool proxy — HIGH-risk capability 강제 + delegation binding + trace.

    Args:
        tool_name: 호출할 MCP tool 이름
        args: tool 인자
        capability_token: decoded JWT dict 또는 JWT 문자열 (HIGH-risk 시 필수)
        context: {
            action, resource, is_external, data_classification,
            trace_id, request_id, session_id,
            delegation_id, credential_binding_id,
            user_id, agent_id, tenant_id
        }

    Returns:
        dict with ok / error, risk, trace_id, tool
    """
    raw_action = context.get("action", "EXECUTE")
    raw_resource = context.get("resource", tool_name)
    is_external = context.get("is_external", False)
    data_classification = context.get("data_classification")
    trace_id = context.get("trace_id") or context.get("traceId") or f"trace_{uuid.uuid4().hex[:12]}"
    request_id = context.get("request_id") or f"req_{uuid.uuid4().hex[:8]}"

    # 1. action/resource 정규화
    try:
        action = canonicalize_action(str(raw_action))
    except ValueError:
        action = str(raw_action).upper().strip()

    try:
        resource = normalize_resource(str(raw_resource))
    except ValueError:
        resource = str(raw_resource)

    # 2. risk 분류 (deterministic)
    risk = classify(
        action,
        resource,
        is_external=is_external,
        data_classification=data_classification,
        arg_hints=args if isinstance(args, dict) else None,
    )
    risk_value = risk.value if hasattr(risk, "value") else str(risk)

    # 3. HIGH-risk는 capability token 필수
    # token 정규화: 문자열이면 decode 시도, dict면 그대로
    token_dict: dict | None = None
    if isinstance(capability_token, str) and capability_token.strip():
        # JWT 문자열 — decode 시도 (signing_key 없이 payload만)
        try:
            from jose import jwt  # type: ignore
            token_dict = jwt.get_unverified_claims(capability_token)
        except Exception:
            # opaque token으로 간주하고 capability 검증은 아래에서 수행
            token_dict = {"raw": capability_token, "action": action, "resource": resource}
    elif isinstance(capability_token, dict):
        token_dict = capability_token
    else:
        token_dict = None

    if risk_value == "HIGH" and not token_dict:
        return {
            "error": "CAPABILITY_REQUIRED",
            "reason": f"HIGH-risk action {action} on {resource} requires capability token",
            "risk": risk_value,
            "trace_id": trace_id,
            "request_id": request_id,
        }

    # 4. capability 검증 (token이 있으면)
    if token_dict is not None:
        # token_dict가 raw opaque이면 간단 검증
        if "raw" in token_dict and token_dict.get("action") == action:
            check = verify_capability(token_dict, action, resource, context)
        else:
            check = verify_capability(token_dict, action, resource, context)
        if not check.allowed:
            return {
                "error": "CAPABILITY_DENIED",
                "reason": check.reason,
                "risk": risk_value,
                "trace_id": trace_id,
                "request_id": request_id,
            }

    # 5. delegation binding trace (감사)
    # 실제 MCP forward는 여기서 수행 — 현재는 stub이므로 성공으로 반환
    # prod에서는 MCP client로 위임 (httpx / stdio)

    # 6. 성공 — trace 전파
    result: dict[str, Any] = {
        "ok": True,
        "risk": risk_value,
        "tool": tool_name,
        "action": action,
        "resource": resource,
        "trace_id": trace_id,
        "request_id": request_id,
    }
    # delegation 정보 trace에 포함
    if context.get("delegation_id"):
        result["delegation_id"] = context["delegation_id"]
    if token_dict and token_dict.get("jti"):
        result["capability_jti"] = token_dict["jti"]
    elif token_dict and token_dict.get("nonce"):
        result["capability_nonce"] = token_dict["nonce"]

    # audit hint (호출자가 audit ledger에 기록할 수 있도록)
    result["audit"] = {
        "tool": tool_name,
        "action": action,
        "resource": resource,
        "risk": risk_value,
        "trace_id": trace_id,
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return result
