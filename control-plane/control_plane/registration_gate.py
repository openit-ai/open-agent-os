"""Deterministic OAOS user-registration gate for Mattermost/Slack ingress.

The administrator's persistent user mapping is the source of truth. This gate
runs before any LLM, Google, Enterprise MCP, or Personal Wiki operation. It
uses Redis for durable per-owner registration state in production and fails
closed when the mapping/Redis dependency cannot be verified.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

_STATES = ("DISCOVERED", "GREETED", "BASIC_READY", "SESSION_OK", "OAUTH_PENDING", "VERIFIED", "CONNECTED")
_NEXT = {
    "DISCOVERED": "GREETED",
    "GREETED": "BASIC_READY",
    "BASIC_READY": "SESSION_OK",
}

@dataclass(frozen=True)
class GateResult:
    allowed: bool
    state: str
    response: str = ""
    reason: str = ""
    data: dict[str, Any] | None = None


def _production() -> bool:
    return os.getenv("OAOS_ENV", "").strip().lower() in {"production", "prod"}


def _key(tenant_id: str, user_id: str) -> str:
    safe_tenant = re.sub(r"[^A-Za-z0-9_.:-]", "_", tenant_id)[:100]
    safe_user = re.sub(r"[^A-Za-z0-9_.:-]", "_", user_id)[:160]
    return f"oaos:registration:{safe_tenant}:{safe_user}"


def _redis_client():
    url = os.getenv("REDIS_URL") or os.getenv("OAOS_REDIS_URL")
    if not url:
        return None
    try:
        import redis
        client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=1.0)
        client.ping()
        return client
    except Exception as exc:
        if _production():
            raise RuntimeError("registration state Redis unavailable") from exc
        return None


_memory: dict[str, dict[str, Any]] = {}


def _load(key: str) -> dict[str, Any] | None:
    client = _redis_client()
    if client is not None:
        raw = client.get(key)
        return json.loads(raw) if raw else None
    if _production():
        raise RuntimeError("registration state backend unavailable")
    return _memory.get(key)


def _save(key: str, value: dict[str, Any]) -> None:
    client = _redis_client()
    if client is not None:
        client.set(key, json.dumps(value, ensure_ascii=False), ex=60 * 60 * 24 * 30)
        return
    if _production():
        raise RuntimeError("registration state backend unavailable")
    _memory[key] = value


def _registered_mapping(tenant_id: str, user_id: str) -> dict[str, str] | None:
    """Read the administrator mapping; no guessed user is accepted."""
    # Tests and deployments may inject the authoritative provider explicitly.
    injected = globals().get("_MAPPING_PROVIDER")
    if callable(injected):
        result = injected(tenant_id, user_id)
        return result if isinstance(result, dict) else None
    try:
        from .user_mapping_lookup import lookup_registered_owner
        return lookup_registered_owner(tenant_id, user_id)
    except ImportError:
        pass
    # Production never infers registration from the payload.
    return None


def handle(*, tenant_id: str, user_id: str, session_id: str, text: str, platform: str | None = None) -> GateResult:
    if platform not in {"mattermost", "slack"}:
        return GateResult(True, "BYPASS", data={"registration_gate": "not_applicable"})
    mapping = _registered_mapping(tenant_id, user_id)
    if mapping is None:
        return GateResult(False, "UNREGISTERED", "현재 계정은 웹관리자 콘솔에 등록되어 있지 않습니다. 관리자에게 사용자 등록을 요청해 주세요.", "admin mapping missing")
    key = _key(tenant_id, user_id)
    record = _load(key) or {"state": "DISCOVERED", "tenant_id": tenant_id, "user_id": user_id, "agent_id": mapping.get("agent_id", ""), "session_id": session_id, "created_at": time.time(), "answers": {}}
    state = str(record.get("state", "DISCOVERED"))
    normalized = (text or "").strip()
    if state == "DISCOVERED":
        record["state"] = "GREETED"
        _save(key, record)
        return GateResult(False, "GREETED", "안녕하세요. OAOS 개인 업무 에이전트입니다. 먼저 사용자 등록과 세션 분리를 진행하겠습니다. 어떻게 불러드리면 될까요?", "registration onboarding", record)
    if state == "GREETED":
        record["state"] = "BASIC_READY"
        record.setdefault("answers", {})["honorific"] = normalized[:80]
        _save(key, record)
        return GateResult(False, "BASIC_READY", "확인했습니다. 답변은 간단하게 드릴까요, 자세히 드릴까요? 그리고 업무 요청 시 결론부터 안내드리면 될까요? (예: 간단하게, 결론부터)", "initial preference collection", record)
    if state == "BASIC_READY":
        record["state"] = "SESSION_OK"
        record.setdefault("answers", {})["response_style"] = normalized[:200]
        _save(key, record)
        return GateResult(False, "SESSION_OK", "기본 설정과 사용자별 OAOS 세션 분리를 확인했습니다. 이제 원하실 때 `구글 워크스페이스 연동 시작`이라고 말씀하시면 본인 계정 OAuth 절차를 진행하겠습니다. 회사 문서·정책 질문은 전사 지식 MCP/Knowledge Index 경로를 기본으로 사용합니다.", "session owner verified", record)
    # OAuth is never advanced by merely mentioning it before SESSION_OK.
    if normalized.lower() in {"구글 워크스페이스 연동 시작", "google workspace oauth", "oauth 시작"} and state not in {"SESSION_OK", "OAUTH_PENDING", "VERIFIED", "CONNECTED"}:
        return GateResult(False, state, "아직 Google OAuth를 시작할 수 없습니다. 먼저 인사말·호칭·초기 응답 설정과 세션 분리를 완료해 주세요.", "SESSION_OK required", record)
    # Enterprise Knowledge MCP is the default for company-policy/document
    # questions after registration; Personal Wiki/Profile remains owner-scoped.
    return GateResult(True, state, data={"registration_gate": "passed", "registration_state": state, "mapping": mapping, "answers": record.get("answers", {}), "knowledge_route": "enterprise_mcp_default", "personal_route": "personal_wiki_profile"})
