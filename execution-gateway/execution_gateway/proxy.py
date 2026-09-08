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
    from jose.exceptions import JWTError as _JWTError  # type: ignore
except ImportError:
    class _JWTError(Exception):
        """Fallback type when python-jose is not installed."""

        ...

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
    except (ImportError, ModuleNotFoundError):
        get_data_access_policy = None  # type: ignore

logger = logging.getLogger(__name__)


def _safe_exception_name(exc: BaseException) -> str:
    """Return a non-sensitive failure label for logs and response metadata."""
    return type(exc).__name__


def _error_response(
    error: str,
    reason: str,
    *,
    status_code: int,
    risk: str,
    trace_id: str,
    request_id: str,
    **extra: Any,
) -> dict[str, Any]:
    """Build the proxy error contract without exposing exception/token contents."""
    return {
        "error": error,
        "reason": reason,
        "status_code": status_code,
        "risk": risk,
        "trace_id": trace_id,
        "request_id": request_id,
        **extra,
    }


def _upstream_status(payload: Any) -> int | None:
    """Extract only supported HTTP status values from an upstream envelope."""
    if not isinstance(payload, dict):
        return None
    value = payload.get("status_code", payload.get("status"))
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return None


def _upstream_error(status_code: int, *, risk: str, trace_id: str, request_id: str) -> dict[str, Any]:
    if status_code == 401:
        error, reason = "UPSTREAM_UNAUTHORIZED", "upstream authentication failed"
    elif status_code == 403:
        error, reason = "UPSTREAM_FORBIDDEN", "upstream permission denied"
    elif status_code == 429:
        error, reason = "UPSTREAM_RATE_LIMITED", "upstream rate limit exceeded"
    elif status_code >= 500:
        error, reason = "UPSTREAM_UNAVAILABLE", "upstream service unavailable"
    else:
        error, reason = "UPSTREAM_ERROR", "upstream request failed"
    return _error_response(error, reason, status_code=status_code, risk=risk, trace_id=trace_id, request_id=request_id)

# fail-closed gate for mock fallback in production — H7 immutable
def _is_mock_allowed() -> bool:
    # delegate to canonical gate first
    try:
        from .env_gate import is_mock_allowed as _g
        return _g()
    except (ImportError, ModuleNotFoundError, AttributeError, TypeError) as exc:
        logger.debug("canonical mock gate unavailable: %s", _safe_exception_name(exc))
    try:
        from execution_gateway.env_gate import is_mock_allowed as _g2  # type: ignore
        return _g2()
    except (ImportError, ModuleNotFoundError, AttributeError, TypeError) as exc:
        logger.debug("execution gateway mock gate unavailable: %s", _safe_exception_name(exc))
    try:
        from agent_runtime.env_gate import is_mock_allowed as _g3  # type: ignore
        return _g3()
    except (ImportError, ModuleNotFoundError, AttributeError, TypeError) as exc:
        logger.debug("agent runtime mock gate unavailable: %s", _safe_exception_name(exc))
    import os as _os
    for k in ("OAOS_ENV","ENV","OAOS_ENVIRONMENT","APP_ENV","ENVIRONMENT"):
        if _os.getenv(k,"").strip().lower() in ("production","prod"):
            return False
    mf = _os.getenv("OAOS_MOCK_FALLBACK","").strip().lower()
    if mf in ("0","false","no","off"):
        return False
    return True

def _is_prod() -> bool:
    try:
        from .env_gate import is_production as _p
        return _p()
    except (ImportError, ModuleNotFoundError, AttributeError, TypeError) as exc:
        logger.debug("canonical production gate unavailable: %s", _safe_exception_name(exc))
        import os
        return os.getenv("OAOS_ENV", "").lower() in ("production", "prod")


def _get_registry():
    """Lazy import to avoid circular dependency at module load."""
    try:
        from .mcp_registry import default_registry
        return default_registry
    except ImportError:
        try:
            from execution_gateway.mcp_registry import default_registry  # type: ignore
            return default_registry
        except (ImportError, ModuleNotFoundError, AttributeError) as exc:
            logger.warning("MCP registry unavailable: %s", _safe_exception_name(exc))
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
    # colleague DM tools
    "notify_colleague": "notify_colleague",
    "mattermost_send_direct_message": "mattermost_send_direct_message",
    "mattermost_send_dm": "mattermost_send_dm",
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
        except (ImportError, ModuleNotFoundError):
            return None
    # Colleague DM tools need full args passthrough
    if method_name in ("notify_colleague", "mattermost_send_direct_message", "mattermost_send_dm"):
        try:
            executor = MockToolExecutor(context)
            method = getattr(executor, method_name, None)
            if not method:
                return None
            result = method(**(args or {}))
            return result if isinstance(result, dict) else {"result": result}
        except (AttributeError, KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
            logger.warning("mock colleague fallback failed for %s: %s", tool_name, _safe_exception_name(exc))
            return {"error": "MOCK_EXECUTION_FAILED", "tool": tool_name}
    try:
        executor = MockToolExecutor(context)
        method = getattr(executor, method_name, None)
        if not method:
            return None
        result = method(**{k: v for k, v in args.items() if k in ("query", "limit", "date", "filter")}) if args else method()
        if not isinstance(result, dict):
            result = {"result": result}
        return result
    except TypeError:
        try:
            executor = MockToolExecutor(context)  # type: ignore
            method = getattr(executor, method_name)
            result = method()
            return result if isinstance(result, dict) else {"result": result}
        except (AttributeError, KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
            logger.warning("mock fallback retry failed for %s: %s", tool_name, _safe_exception_name(exc))
            return None
    except (AttributeError, KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
        logger.warning("mock fallback failed for %s: %s", tool_name, _safe_exception_name(exc))
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
        if not isinstance(raw, dict):
            return None, "malformed upstream response"
        return raw, None
    except (ConnectionError, TimeoutError, OSError, RuntimeError, ValueError, TypeError) as exc:
        # Preserve the mock/no-transport distinction, but never return raw exception text.
        kind = _safe_exception_name(exc)
        if isinstance(exc, RuntimeError) and "mock" in kind.lower():
            return None, "mock transport unavailable"
        return None, f"upstream transport unavailable ({kind})"


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
    except (TypeError, ValueError):
        return _error_response(
            "INVALID_REQUEST",
            "invalid action",
            status_code=422,
            risk="UNKNOWN",
            trace_id=trace_id,
            request_id=request_id,
        )
    try:
        resource = normalize_resource(str(raw_resource))
    except (TypeError, ValueError):
        return _error_response(
            "INVALID_REQUEST",
            "invalid resource",
            status_code=422,
            risk="UNKNOWN",
            trace_id=trace_id,
            request_id=request_id,
        )

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
                    "status_code": 403,
                    "risk": "HIGH",
                    "trace_id": trace_id,
                    "request_id": request_id,
                    "data_access": {"decision": _da.decision, "reason": _da.reason, "resource": resource, "action": action},
                }
            # store for later audit attachment
            _data_access_hint = {"decision": _da.decision, "reason": _da.reason, "required_source": _da.required_source}
        except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
            logger.warning("data access policy backend unavailable: %s", _safe_exception_name(exc))
            return _error_response(
                "BACKEND_UNAVAILABLE",
                "data access policy unavailable",
                status_code=503,
                risk="HIGH",
                trace_id=trace_id,
                request_id=request_id,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("data access policy response invalid: %s", _safe_exception_name(exc))
            return _error_response(
                "BACKEND_UNAVAILABLE",
                "data access policy response invalid",
                status_code=503,
                risk="HIGH",
                trace_id=trace_id,
                request_id=request_id,
            )
    else:
        if _is_prod():
            return _error_response(
                "BACKEND_UNAVAILABLE",
                "data access policy unavailable",
                status_code=503,
                risk="HIGH",
                trace_id=trace_id,
                request_id=request_id,
            )
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
    # Colleague DM is internal — approval not required (§14), keep audit but exempt HIGH capability
    _colleague_tools = {"notify_colleague", "mattermost_send_direct_message", "mattermost_send_dm"}
    if tool_name in _colleague_tools or resource.startswith("mattermost/dm"):
        # Force LOW risk so capability not required
        from execution_gateway.risk import RiskLevel as _RL  # type: ignore
        risk = _RL.LOW  # type: ignore
        risk_value = "LOW"

    # 3. HIGH-risk는 capability token 필수
    # token 정규화: 문자열이면 decode 시도, dict면 그대로
    # Stage4: enforce signature + replay for JWT string tokens via TokenService/signing_key when available
    token_dict: dict | None = None
    _token_string_raw: str | None = None
    _token_verify_error: str | None = None
    if isinstance(capability_token, str) and capability_token.strip():
        _token_string_raw = capability_token.strip()
        import os as _os_verify

        _signing_key = (
            _os_verify.getenv("OAOS_SECURITY_SERVICE_SIGNING_KEY")
            or _os_verify.getenv("OAOS_SIGNING_KEY")
            or _os_verify.getenv("OAOS_CAPABILITY_SIGNING_KEY")
            or _os_verify.getenv("OAOS_AUDIT_SIGNING_KEY")
        )
        if not _signing_key:
            try:
                from security.app import get_signing_key as _gsk  # type: ignore
                _signing_key = _gsk()
            except (ImportError, ModuleNotFoundError, AttributeError, TypeError) as exc:
                logger.debug("signing key provider unavailable: %s", _safe_exception_name(exc))
            except RuntimeError as exc:
                logger.warning("signing key backend unavailable: %s", _safe_exception_name(exc))
                return _error_response(
                    "BACKEND_UNAVAILABLE",
                    "capability token backend unavailable",
                    status_code=503,
                    risk=risk_value,
                    trace_id=trace_id,
                    request_id=request_id,
                )

        if not _signing_key and _is_prod():
            return _error_response(
                "INVALID_CREDENTIAL",
                "capability token verification is unavailable",
                status_code=401,
                risk=risk_value,
                trace_id=trace_id,
                request_id=request_id,
            )

        if not _signing_key:
            # Explicit non-production compatibility path for test/development JWTs.
            try:
                from jose import jwt as _jwt  # type: ignore
                token_dict = _jwt.get_unverified_claims(_token_string_raw)
            except (ImportError, ModuleNotFoundError, _JWTError, ValueError, TypeError, KeyError) as exc:
                logger.warning("non-production capability token parsing failed: %s", _safe_exception_name(exc))
                return _error_response(
                    "INVALID_CREDENTIAL",
                    "invalid capability token",
                    status_code=401,
                    risk=risk_value,
                    trace_id=trace_id,
                    request_id=request_id,
                )
        else:
            _verify_fn = None
            try:
                from token_service.service import verify_capability_token as _verify_fn  # type: ignore
            except (ImportError, ModuleNotFoundError):
                try:
                    from token.token_service.service import verify_capability_token as _verify_fn  # type: ignore
                except (ImportError, ModuleNotFoundError):
                    _verify_fn = None

            try:
                if _verify_fn is not None:
                    token_dict = _verify_fn(_signing_key, _token_string_raw)
                else:
                    from jose import jwt as _jwt_verified  # type: ignore
                    token_dict = _jwt_verified.decode(_token_string_raw, _signing_key, algorithms=["HS256"])
            except RuntimeError as exc:
                message = str(exc).lower()
                if "replay" in message or "revoked" in message or "expired" in message:
                    _token_verify_error = "capability token rejected"
                    logger.info("capability token rejected: %s", _safe_exception_name(exc))
                    return _error_response(
                        "INVALID_CREDENTIAL",
                        _token_verify_error,
                        status_code=401,
                        risk=risk_value,
                        trace_id=trace_id,
                        request_id=request_id,
                    )
                logger.warning("capability token backend unavailable: %s", _safe_exception_name(exc))
                return _error_response(
                    "BACKEND_UNAVAILABLE",
                    "capability token backend unavailable",
                    status_code=503,
                    risk=risk_value,
                    trace_id=trace_id,
                    request_id=request_id,
                )
            except (_JWTError, ValueError, TypeError, KeyError, OSError, ImportError, ModuleNotFoundError) as exc:
                logger.info("capability token rejected: %s", _safe_exception_name(exc))
                if not _is_prod():
                    try:
                        from jose import jwt as _jwt_unverified  # type: ignore
                        token_dict = _jwt_unverified.get_unverified_claims(_token_string_raw)
                    except (ImportError, ModuleNotFoundError, _JWTError, ValueError, TypeError, KeyError):
                        return _error_response(
                            "INVALID_CREDENTIAL",
                            "invalid capability token",
                            status_code=401,
                            risk=risk_value,
                            trace_id=trace_id,
                            request_id=request_id,
                        )
                else:
                    return _error_response(
                        "INVALID_CREDENTIAL",
                        "invalid capability token",
                        status_code=401,
                        risk=risk_value,
                        trace_id=trace_id,
                        request_id=request_id,
                    )

        if not isinstance(token_dict, dict):
            return _error_response(
                "INVALID_CREDENTIAL",
                "invalid capability token payload",
                status_code=401,
                risk=risk_value,
                trace_id=trace_id,
                request_id=request_id,
            )

        # Proxy-level replay guard is retained for non-production unverified JWTs.
        _proxy_jti = token_dict.get("jti")
        _proxy_nonce = token_dict.get("nonce")
        global _PROXY_SEEN_JTIS, _PROXY_SEEN_NONCES
        if "_PROXY_SEEN_JTIS" not in globals():
            globals()["_PROXY_SEEN_JTIS"] = set()
            globals()["_PROXY_SEEN_NONCES"] = set()
        if _proxy_jti is not None and _proxy_jti in globals()["_PROXY_SEEN_JTIS"]:
            return _error_response("CAPABILITY_DENIED", "token replay detected", status_code=401, risk=risk_value, trace_id=trace_id, request_id=request_id)
        if _proxy_nonce is not None and _proxy_nonce in globals()["_PROXY_SEEN_NONCES"]:
            return _error_response("CAPABILITY_DENIED", "token replay detected", status_code=401, risk=risk_value, trace_id=trace_id, request_id=request_id)
    elif isinstance(capability_token, dict):
        token_dict = capability_token
    else:
        token_dict = None

    if risk_value == "HIGH" and not token_dict:
        return _error_response(
            "CAPABILITY_REQUIRED",
            f"HIGH-risk action {action} on {resource} requires capability token",
            status_code=401,
            risk=risk_value,
            trace_id=trace_id,
            request_id=request_id,
        )

    # 4. capability 검증 (token이 있으면)
    if token_dict is not None:
        # token_dict가 raw opaque이면 간단 검증
        try:
            check = verify_capability(token_dict, action, resource, context)
        except (AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
            logger.warning("capability evaluation failed: %s", _safe_exception_name(exc))
            return _error_response(
                "POLICY_UNAVAILABLE",
                "capability evaluation unavailable",
                status_code=503,
                risk=risk_value,
                trace_id=trace_id,
                request_id=request_id,
            )
        if not check.allowed:
            reason = check.reason if isinstance(check.reason, str) else "capability denied"
            return _error_response(
                "CAPABILITY_DENIED",
                reason,
                status_code=403,
                risk=risk_value,
                trace_id=trace_id,
                request_id=request_id,
            )
        # record string token replay after successful capability check
        if _token_string_raw is not None:
            _proxy_jti2 = token_dict.get("jti") if isinstance(token_dict, dict) else None
            _proxy_nonce2 = token_dict.get("nonce") if isinstance(token_dict, dict) else None
            if "_PROXY_SEEN_JTIS" not in globals():
                globals()["_PROXY_SEEN_JTIS"] = set()
                globals()["_PROXY_SEEN_NONCES"] = set()
            if _proxy_jti2 is not None:
                globals()["_PROXY_SEEN_JTIS"].add(_proxy_jti2)
            if _proxy_nonce2 is not None:
                globals()["_PROXY_SEEN_NONCES"].add(_proxy_nonce2)

    # 5. MCP transport 라우팅 — capability 검증 후 실제 transport로
    transport_result: dict | None = None
    transport_error: str | None = None
    use_mock_fallback = context.get("use_mock_fallback", True)
    # Allow caller to disable mock fallback explicitly
    if context.get("force_transport") or not use_mock_fallback:
        # Strict: must succeed via transport
        tr, err = await _try_transport_call(tool_name, args, context)
        if tr is not None:
            upstream_status = _upstream_status(tr)
            if upstream_status is not None and upstream_status >= 400:
                return _upstream_error(upstream_status, risk=risk_value, trace_id=trace_id, request_id=request_id)
            transport_result = tr
        else:
            # Check if error is "mock" vs real transport failure
            if err and "mock" in err.lower():
                if not use_mock_fallback:
                    return _error_response(
                        "TRANSPORT_REQUIRED",
                        f"tool {tool_name} has no real transport",
                        status_code=503,
                        risk=risk_value,
                        trace_id=trace_id,
                        request_id=request_id,
                    )
                # fallback to mock below
            else:
                status = 502 if err == "malformed upstream response" else 503
                return _error_response(
                    "UPSTREAM_MALFORMED" if status == 502 else "UPSTREAM_UNAVAILABLE",
                    "malformed upstream response" if status == 502 else "upstream service unavailable",
                    status_code=status,
                    risk=risk_value,
                    trace_id=trace_id,
                    request_id=request_id,
                )
    else:
        # Default: try transport, fallback to mock on failure
        tr, err = await _try_transport_call(tool_name, args, context)
        if tr is not None:
            upstream_status = _upstream_status(tr)
            if upstream_status is not None and upstream_status >= 400:
                return _upstream_error(upstream_status, risk=risk_value, trace_id=trace_id, request_id=request_id)
            transport_result = tr
        else:
            transport_error = err  # record for debug, but continue to mock

    mock_result: dict | None = None
    if transport_result is None:
        # 6. mock fallback (MCP 서버 없을 때) — fail-closed in production
        if not _is_mock_allowed():
            return _error_response(
                "MOCK_FALLBACK_DISABLED",
                "mock fallback is disabled",
                status_code=503,
                risk=risk_value,
                trace_id=trace_id,
                request_id=request_id,
                code="MOCK_FALLBACK_DISABLED",
            )
        mock_result = _mock_fallback(tool_name, args, context)
        # Never report success without a real or mock execution result.
        if mock_result is None:
            return _error_response(
                "UPSTREAM_UNAVAILABLE",
                "tool execution unavailable",
                status_code=503,
                risk=risk_value,
                trace_id=trace_id,
                request_id=request_id,
                code="TRANSPORT_UNAVAILABLE",
            )

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

    # Personal Wiki auto-archive hook (best-effort, non-blocking) — after mock fallback / proxy result
    try:
        from .wiki_archive import auto_archive  # type: ignore
    except ImportError:
        try:
            from execution_gateway.wiki_archive import auto_archive  # type: ignore
        except (ImportError, ModuleNotFoundError):
            auto_archive = None  # type: ignore
    if "auto_archive" in locals() and auto_archive is not None:
        try:
            # truncate result to 4k chars inside auto_archive; pass full result
            auto_archive(trace_id=trace_id, tool_name=tool_name, result=result, max_chars=4000)
        except (AttributeError, KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
            # Archive is optional; preserve the primary tool result and expose degraded state in logs.
            logger.warning("optional auto-archive degraded: %s", _safe_exception_name(exc))
            result["archive"] = {"status": "degraded"}

    return result
