"""Mock Tool Executor — Morning Briefing (Section 3.1)

Implements 6 mock tools that propagate trace_id and create audit events:
  gmail.search, calendar.list, tasks.list, drive.recent, outline.search, mattermost.mentions

Each call:
  - propagates trace_id from AgentContext
  - creates AuditEvent with hash-chain via AuditLedger
  - returns per-user mock data (owner isolation)

Used by examples/morning-briefing/orchestrator.py and control-plane demo endpoint.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

# Audit ledger imports — try multiple paths
try:
    from audit.audit_ledger.ledger import AuditLedger
    from audit_model import AuditEvent, AuditEventType
except Exception:
    try:
        from audit_model.model import AuditEvent, AuditEventType  # type: ignore
        from security.audit.audit_ledger.ledger import AuditLedger  # type: ignore
    except Exception:
        AuditLedger = None  # type: ignore
        AuditEvent = None  # type: ignore
        AuditEventType = None  # type: ignore

# Global ledger singleton for demo — tests can inspect verify_chain()
_ledger: Any = None


def get_ledger() -> Any:
    global _ledger
    if _ledger is None and AuditLedger is not None:
        _ledger = AuditLedger(signing_key="demo-audit-key")
    return _ledger


def reset_ledger() -> None:
    global _ledger
    if AuditLedger is not None:
        _ledger = AuditLedger(signing_key="demo-audit-key")
    else:
        _ledger = None


# ── Per-user mock datasets ────────────────────────────────────────────
# kim has rich data for Section 3.1 scenario; lee has minimal different data

_NOW = datetime.now(timezone.utc)

_MOCK_CALENDAR: dict[str, list[dict]] = {
    "employee:kim": [
        {"id": "cal_kim_1", "title": "고객 A 미팅", "start": "09:30", "end": "10:30", "location": "회의실 A", "attendees": ["고객 A", "김대리"], "description": "A사 제안서 검토"},
        {"id": "cal_kim_2", "title": "개발회의", "start": "11:00", "end": "12:00", "location": "개발 회의실", "attendees": ["개발팀", "김대리"], "description": "주간 스프린트"},
        {"id": "cal_kim_3", "title": "점심 식사", "start": "12:30", "end": "13:30", "location": "사내 식당"},
        {"id": "cal_kim_4", "title": "내부 리뷰", "start": "14:00", "end": "15:00", "location": "회의실 B"},
    ],
    "employee:lee": [
        {"id": "cal_lee_1", "title": "영업 미팅", "start": "10:00", "end": "11:00", "location": "회의실 C", "attendees": ["이과장"]},
    ],
}

_MOCK_GMAIL: dict[str, list[dict]] = {
    "employee:kim": [
        {"id": "mail_kim_1", "from": "customer-a@example.com", "subject": "A사 제안서 피드백", "snippet": "제안서 잘 받았습니다...", "unread": True, "thread_id": "thr_1"},
        {"id": "mail_kim_2", "from": "customer-a@example.com", "subject": "Re: A사 제안서 피드백", "snippet": "추가 자료 요청", "unread": True, "thread_id": "thr_1"},
        {"id": "mail_kim_3", "from": "customer-a@example.com", "subject": "Re: A사 제안서 피드백 - 일정", "snippet": "09:30 미팅 확정", "unread": True, "thread_id": "thr_1"},
        {"id": "mail_kim_4", "from": "b-company@example.com", "subject": "B사 견적 문의 — 회신 필요", "snippet": "견적서 요청드립니다", "unread": True, "needs_reply": True},
        {"id": "mail_kim_5", "from": "team@example.com", "subject": "주간 보고서 제출 안내", "snippet": "금요일까지 제출", "unread": True, "needs_reply": True},
        {"id": "mail_kim_6", "from": "dev@example.com", "subject": "Issue #42 업데이트", "snippet": "버그 수정 완료", "unread": False},
        {"id": "mail_kim_7", "from": "noreply@example.com", "subject": "시스템 알림", "snippet": "배포 완료", "unread": False},
    ],
    "employee:lee": [
        {"id": "mail_lee_1", "from": "customer-b@example.com", "subject": "B사 계약 문의", "snippet": "계약서 검토", "unread": True},
    ],
}

_MOCK_TASKS: dict[str, list[dict]] = {
    "employee:kim": [
        {"id": "task_kim_1", "title": "B사 회신", "due": "today", "status": "pending", "priority": "high"},
        {"id": "task_kim_2", "title": "보고서 제출", "due": "today", "status": "pending", "priority": "high"},
        {"id": "task_kim_3", "title": "코드 리뷰", "due": "tomorrow", "status": "pending", "priority": "medium"},
    ],
    "employee:lee": [
        {"id": "task_lee_1", "title": "영업 보고서", "due": "today", "status": "pending"},
    ],
}

_MOCK_DRIVE: dict[str, list[dict]] = {
    "employee:kim": [
        {"id": "drive_kim_1", "name": "A사_제안서_v3.pdf", "modified": (_NOW - timedelta(hours=2)).isoformat(), "owner": "employee:kim", "url": "drive/user/kim/files/A사_제안서_v3"},
        {"id": "drive_kim_2", "name": "주간보고서_초안.docx", "modified": (_NOW - timedelta(hours=5)).isoformat(), "owner": "employee:kim", "url": "drive/user/kim/files/주간보고서"},
    ],
    "employee:lee": [
        {"id": "drive_lee_1", "name": "영업자료.pdf", "modified": _NOW.isoformat(), "owner": "employee:lee", "url": "drive/user/lee/files/영업자료"},
    ],
}

_MOCK_OUTLINE: dict[str, list[dict]] = {
    # Outline is shared but filtered by ACL — different per user for isolation demo
    "employee:kim": [
        {"id": "outline_kim_1", "title": "A 프로젝트 문서", "collection": "team", "url": "outline/team/a-project", "snippet": "A사 고객 요구사항..."},
        {"id": "outline_kim_2", "title": "개발 위키 — 스프린트", "collection": "team", "url": "outline/team/sprint", "snippet": "이번 스프린트 목표..."},
    ],
    "employee:lee": [
        {"id": "outline_lee_1", "title": "영업 가이드", "collection": "team", "url": "outline/team/sales-guide", "snippet": "영업 프로세스..."},
    ],
}

_MOCK_MATTERMOST: dict[str, list[dict]] = {
    "employee:kim": [
        {"id": "mm_kim_1", "channel": "dev", "from": "박팀장", "text": "@kim 어제 논의한 Issue 2건 확인 부탁", "timestamp": (_NOW - timedelta(hours=12)).isoformat()},
        {"id": "mm_kim_2", "channel": "general", "from": "이과장", "text": "@kim 고객 A 미팅 자료 공유 부탁", "timestamp": (_NOW - timedelta(hours=10)).isoformat()},
        {"id": "mm_kim_3", "channel": "dev", "from": "최대리", "text": "@kim PR 리뷰 요청", "timestamp": (_NOW - timedelta(hours=8)).isoformat()},
    ],
    "employee:lee": [
        {"id": "mm_lee_1", "channel": "sales", "from": "김대리", "text": "@lee 계약서 검토", "timestamp": _NOW.isoformat()},
    ],
}

_MOCK_CRM: dict[str, list[dict]] = {
    "employee:kim": [
        {"id": "crm_kim_1", "customer": "고객 A", "last_contact": (_NOW - timedelta(days=2)).isoformat(), "status": "미팅 예정 09:30", "notes": "제안서 v3 전달 완료"},
    ],
    "employee:lee": [
        {"id": "crm_lee_1", "customer": "고객 B", "last_contact": _NOW.isoformat(), "status": "견적 대기"},
    ],
}


def _audit(tool_name: str, action: str, resource: str, ctx: dict, result_count: int = 0) -> None:
    """Append audit event to global ledger with trace_id propagation."""
    ledger = get_ledger()
    if ledger is None or AuditEvent is None:
        return
    try:
        ev = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type=AuditEventType.MCP_TOOL_CALL if hasattr(AuditEventType, "MCP_TOOL_CALL") else AuditEventType.DATA_ACCESS,  # type: ignore
            timestamp=datetime.now(timezone.utc),
            tenant_id=ctx.get("tenant_id", "default"),
            user_id=ctx.get("user_id"),
            agent_id=ctx.get("agent_id"),
            session_id=ctx.get("session_id"),
            trace_id=ctx.get("trace_id"),
            request_id=ctx.get("request_id"),
            resource=resource,
            action=action,
            tool_name=tool_name,
            decision="ALLOW",
        )
        ledger.append(ev)
        # Also append a DATA_ACCESS event for richer chain
        ev2 = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type=AuditEventType.DATA_ACCESS if hasattr(AuditEventType, "DATA_ACCESS") else AuditEventType.MCP_TOOL_CALL,  # type: ignore
            timestamp=datetime.now(timezone.utc),
            tenant_id=ctx.get("tenant_id", "default"),
            user_id=ctx.get("user_id"),
            agent_id=ctx.get("agent_id"),
            session_id=ctx.get("session_id"),
            trace_id=ctx.get("trace_id"),
            request_id=ctx.get("request_id"),
            resource=resource,
            action=action,
            tool_name=tool_name,
            decision="ALLOW",
        )
        ledger.append(ev2)
    except Exception:
        pass  # audit failure must not block tool execution


class MockToolExecutor:
    """Per-request executor — propagates trace_id, creates audit events."""

    def __init__(self, agent_context: dict):
        self.ctx = agent_context
        self.trace_id = agent_context.get("trace_id", f"trace_{uuid.uuid4().hex[:12]}")
        self.user_id = agent_context.get("user_id", "employee:unknown")

    def _get_for_user(self, store: dict[str, list[dict]]) -> list[dict]:
        return list(store.get(self.user_id, []))

    def gmail_search(self, query: str | None = None, limit: int = 10) -> dict:
        resource = f"gmail/user/{self.user_id.split(':')[-1]}/*" if ":" in self.user_id else "gmail/user/unknown/*"
        _audit("gmail.search", "SEARCH", resource, {**self.ctx, "trace_id": self.trace_id}, 0)
        data = self._get_for_user(_MOCK_GMAIL)
        if query:
            data = [m for m in data if query.lower() in m.get("subject", "").lower() or query.lower() in m.get("snippet", "").lower()]
        data = data[:limit]
        return {"tool": "gmail.search", "trace_id": self.trace_id, "items": data, "count": len(data)}

    def calendar_list(self, date: str | None = None) -> dict:
        uid = self.user_id.split(":")[-1] if ":" in self.user_id else "unknown"
        resource = f"calendar/user/{uid}/*"
        _audit("calendar.list", "SEARCH", resource, {**self.ctx, "trace_id": self.trace_id}, 0)
        data = self._get_for_user(_MOCK_CALENDAR)
        return {"tool": "calendar.list", "trace_id": self.trace_id, "items": data, "count": len(data)}

    def tasks_list(self, filter: str | None = None) -> dict:
        uid = self.user_id.split(":")[-1] if ":" in self.user_id else "unknown"
        resource = f"tasks/user/{uid}/*"
        _audit("tasks.list", "SEARCH", resource, {**self.ctx, "trace_id": self.trace_id}, 0)
        data = self._get_for_user(_MOCK_TASKS)
        if filter == "today":
            data = [t for t in data if t.get("due") == "today"]
        return {"tool": "tasks.list", "trace_id": self.trace_id, "items": data, "count": len(data)}

    def drive_recent(self, limit: int = 10) -> dict:
        uid = self.user_id.split(":")[-1] if ":" in self.user_id else "unknown"
        resource = f"drive/user/{uid}/*"
        _audit("drive.recent", "SEARCH", resource, {**self.ctx, "trace_id": self.trace_id}, 0)
        data = self._get_for_user(_MOCK_DRIVE)[:limit]
        return {"tool": "drive.recent", "trace_id": self.trace_id, "items": data, "count": len(data)}

    def outline_search(self, query: str | None = None) -> dict:
        # Outline is shared — but mock per-user for isolation demo
        resource = "outline/team/*"
        _audit("outline.search", "SEARCH", resource, {**self.ctx, "trace_id": self.trace_id}, 0)
        data = self._get_for_user(_MOCK_OUTLINE)
        if query:
            data = [d for d in data if query.lower() in d.get("title", "").lower()]
        return {"tool": "outline.search", "trace_id": self.trace_id, "items": data, "count": len(data)}

    def mattermost_mentions(self) -> dict:
        # Use mattermost resource — not personal, but still filtered by user
        resource = "mattermost/channel/*"
        _audit("mattermost.mentions", "SEARCH", resource, {**self.ctx, "trace_id": self.trace_id}, 0)
        data = self._get_for_user(_MOCK_MATTERMOST)
        return {"tool": "mattermost.mentions", "trace_id": self.trace_id, "items": data, "count": len(data)}

    def crm_search(self) -> dict:
        resource = "crm/customer/*"
        _audit("crm.search", "SEARCH", resource, {**self.ctx, "trace_id": self.trace_id}, 0)
        data = self._get_for_user(_MOCK_CRM)
        return {"tool": "crm.search", "trace_id": self.trace_id, "items": data, "count": len(data)}

    def export_data(self, resource: str = "gmail/user/kim/messages") -> dict:
        """High-risk EXPORT — always HIGH risk, should be APPROVAL_REQUIRED or DENY."""
        _audit("export.data", "EXPORT", resource, {**self.ctx, "trace_id": self.trace_id}, 0)
        return {"tool": "export.data", "trace_id": self.trace_id, "error": "EXPORT blocked", "count": 0}
