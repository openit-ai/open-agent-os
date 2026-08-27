"""Privileged Tool Proxy — Section 7.2 강화

- capability token 검증 + delegation binding + trace 전파
- HIGH-risk는 token 필수
- resource/action normalization
- risk 분류 + audit trace 유지
- P1-2: 실제 MCP transport 라우팅 + mock fallback
"""
from __future__ import annotations

import logging
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

# §16I Data Access hook (deterministic, stub possible)
try:
    from .data_access import get_data_access_policy  # type: ignore
except ImportError:
    try:
        from execution_gateway.data_access import get_data_access_policy  # type: ignore
    except Exception:
        get_data_access_policy = None  # type: ignore

logger = logging.getLogger(__name__)


def _get_registry():
    """Lazy import to avoid circular dependency at module load."""
    try:
        from .mcp_registry import default_registry
        return default_registry
    except ImportError:
        try:
            from execution_gateway.mcp_registry import default_registry  # type: ignore
            return default_registry
        except Exception:
            return None


# Map MCP tool names to MockToolExecutor method names
_TOOL_TO_MOCK: dict[str, str] = {
    "gmail_search": "gmail_search",
    "gmail_read": "gmail_search",
    "gmail_send": "gmail_search",
    "calendar_list": "calendar_list",
    "calendar_read": "calendar_list",
    "calendar_create": "calendar_list",
    "calendar_modify": "calendar_list",
    "drive_search": "drive_recent",
    "drive_read": "drive_recent",
    "drive.recent": "drive_recent",
    "tasks_list": "tasks_list",
    "tasks_create": "tasks_list",
    "tasks_modify": "tasks_list",
    "outline_search": "outline_search",
    "outline_read": "outline_search",
    "outline_create": "outline_search",
    "outline_modify": "outline_search",
    "mattermost.mentions": "mattermost_mentions",
    "crm_search": "crm_search",
    # dot-style variants
    "gmail.search": "gmail_search",
    "calendar.list": "calendar_list",
    "tasks.list": "tasks_list",
    "drive.recent": "drive_recent",
    "outline.search": "outline_search",
}


def _mock_fallback(tool_name: str, args: dict, context: dict) -> dict | None:
    """Execute via MockToolExecutor if tool is known. Returns result dict or None."""
    method_name = _TOOL_TO_MOCK.get(tool_name)
    if not method_name:
        # Try generic: replace _ with . and vice versa
        alt = tool_name.replace("_", ".")
        method_name = _TOOL_TO_MOCK.get(alt)
        if not method_name:
            alt2 = tool_name.replace(".", "_")
            method_name = _TOOL_TO_MOCK.get(alt2)
    if not method_name:
        return None
    try:
        from .mock_executor import MockToolExecutor  # type: ignore
    except ImportError:
        try:
            from execution_gateway.mock_executor import MockToolExecutor  # type: ignore
        except Exception:
            return None
    try:
        executor = MockToolExecutor(context)
        method = getattr(executor, method_name, None)
        if not method:
            return None
        # Call with appropriate args — mock methods accept different signatures
        # We introspect: if args contains query/limit, pass them
        result = method(**{k: v for k, v in args.items() if k in ("query", "limit", "date", "filter")}) if args else method()
        # If method doesn't accept kwargs, try positional fallback
        if not isinstance(result, dict):
            result = {"result": result}
        return result
    except TypeError:
        # Signature mismatch — try no-arg call
        try:
            executor = MockToolExecutor(context)  # type: ignore
            method = getattr(executor, method_name)
            result = method()
            return result if isinstance(result, dict) else {"result": result}
        except Exception as e:
            logger.debug("mock fallback TypeError for %s: %s", tool_name, e)
            return None
    except Exception as e:
        logger.debug("mock fallback failed for %s: %s", tool_name, e)
        return None


async def _try_transport_call(tool_name: str, args: dict, context: dict) -> tuple[dict | None, str | None]:
    """Attempt real MCP transport call. Returns (result, error)."""
    registry = _get_registry()
    if registry is None:
        return None, "no registry"
    server = registry.find_tool(tool_name)
    if server is None:
        return None, f"unknown tool: {tool_name}"
    # If server is mock or has no real transport, signal fallback
    if server.transport == "mock":
        return None, "mock transport — use fallback"
    # Check if transport instance exists (url/command present)
    transport = server.get_transport() if hasattr(server, "get_transport") else None
    if transport is None:
        return None, "no transport — fallback"
    try:
        raw = await server.call_tool(tool_name, args)
        return raw, None
    except Exception as e:
        # Import error type check — MCPTransportError or generic
        err_msg = str(e)
        # If it's a "mock" or "not connected" error, allow fallback
        if "mock" in err_msg.lower() or "not connected" in err_msg.lower():
            return None, err_msg
        # Real transport error — surface it but allow caller to decide fallback
        return None, err_msg


async def proxy_tool_call(
    tool_name: str,
    args: dict,
    capability_token: dict | str | None,
    context: dict,
) -> dict:
    """Tool proxy — HIGH-risk capability 강제 + delegation binding + trace + MCP routing.

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
        dict with ok / error, risk, trace_id, tool, (optional) transport_result / mock_result
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

    # 1b. §16I Data Access hook — 결정론적 (stub 가능)
    # Direct DB / Production 접근은 즉시 DENY, 그 외는 read/write 소스 검증
    if get_data_access_policy is not None:
        try:
            _policy = get_data_access_policy()
            _da = _policy.check(action, resource, source=context.get("data_source") or context.get("source"), user=context.get("user_id"))
            # direct_db / blast radius DENY만 hard block, 그 외는 audit 힌트로만 기록
            if _da.decision == "DENY" and ("direct_db" in _da.reason.lower() or "direct" in _da.reason.lower() or "blast_radius" in _da.reason.lower() or "production" in resource.lower()):
                return {
                    "error": "DATA_ACCESS_DENIED",
                    "reason": _da.reason,
                    "risk": "HIGH",
                    "trace_id": trace_id,
                    "request_id": request_id,
                    "data_access": {"decision": _da.decision, "reason": _da.reason, "resource": resource, "action": action},
                }
            # store for later audit attachment
            _data_access_hint = {"decision": _da.decision, "reason": _da.reason, "required_source": _da.required_source}
        except Exception as e:
            logger.debug("data_access check failed: %s", e)
            _data_access_hint = None
    else:
        _data_access_hint = None

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

    # 5. MCP transport 라우팅 — capability 검증 후 실제 transport로
    transport_result: dict | None = None
    transport_error: str | None = None
    use_mock_fallback = context.get("use_mock_fallback", True)
    # Allow caller to disable mock fallback explicitly
    if context.get("force_transport") or not use_mock_fallback:
        # Strict: must succeed via transport
        tr, err = await _try_transport_call(tool_name, args, context)
        if tr is not None:
            transport_result = tr
        else:
            # Check if error is "mock" vs real transport failure
            if err and "mock" in err.lower():
                if not use_mock_fallback:
                    return {
                        "error": "TRANSPORT_REQUIRED",
                        "reason": f"tool {tool_name} has no real transport: {err}",
                        "risk": risk_value,
                        "trace_id": trace_id,
                        "request_id": request_id,
                    }
                # fallback to mock below
            else:
                return {
                    "error": "TRANSPORT_ERROR",
                    "reason": err or "transport call failed",
                    "risk": risk_value,
                    "trace_id": trace_id,
                    "request_id": request_id,
                }
    else:
        # Default: try transport, fallback to mock on failure
        tr, err = await _try_transport_call(tool_name, args, context)
        if tr is not None:
            transport_result = tr
        else:
            transport_error = err  # record for debug, but continue to mock

    mock_result: dict | None = None
    if transport_result is None:
        # 6. mock fallback (MCP 서버 없을 때)
        mock_result = _mock_fallback(tool_name, args, context)
        # If mock also returned None, we still succeed with stub (backward compat for unknown tools)
        # But if tool was found via registry as mock, mock_result should have data for known tools

    # 7. 성공 — trace 전파
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

    # Attach execution results
    if transport_result is not None:
        result["transport"] = "real"
        result["transport_result"] = transport_result
        # Normalize common MCP content envelope: {content: [{type,text}]} or {result:...}
        result["data"] = transport_result
    elif mock_result is not None:
        result["transport"] = "mock"
        result["mock_result"] = mock_result
        result["data"] = mock_result
        if transport_error:
            result["transport_error"] = transport_error
    else:
        # Stub success — tool not in mock map, but capability/risk checks passed
        result["transport"] = "stub"
        result["data"] = {"tool": tool_name, "args": args}
        if transport_error:
            result["transport_error"] = transport_error

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
    # §16I data_access hint attach
    if "_data_access_hint" in locals() and _data_access_hint:
        result["data_access"] = _data_access_hint
        result["audit"]["data_access"] = _data_access_hint

    return result
