"""Morning Briefing Orchestrator — Section 3.1

User: "오늘 내가 처리해야 할 업무 정리해줘"
  → Calendar + Email + Mattermost + Tasks + Drive + Outline + CRM 종합
  → 09:30 고객미팅 / 11:00 개발회의 / 오늘 반드시 처리 형식

Each source's mock data is generated per-user, but actual Execution Gateway
normalize / authz_hook / policy_engine calls perform authorization:
  - personal resources (gmail/calendar/drive/tasks): owner isolation via GoogleConnector
  - outline: ACL via OutlineConnector
  - enterprise: PolicyEngine (Explicit Deny override)

LOW/MEDIUM → allow; HIGH → APPROVAL_REQUIRED (no capability token issuance in demo).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

# Ensure imports work both when run as module and when imported via tests/control-plane
try:
    from execution_gateway.normalize import normalize_resource, canonicalize_action, is_personal_resource
    from execution_gateway.authz_hook import AuthorizationHook
    from execution_gateway.risk import classify as risk_classify
    from execution_gateway.mock_executor import MockToolExecutor, get_ledger
except ImportError:
    try:
        from execution_gateway.normalize import normalize_resource, canonicalize_action, is_personal_resource  # type: ignore
        from execution_gateway.authz_hook import AuthorizationHook  # type: ignore
        from execution_gateway.risk import classify as risk_classify  # type: ignore
        from execution_gateway.mock_executor import MockToolExecutor, get_ledger  # type: ignore
    except Exception:
        normalize_resource = None  # type: ignore
        canonicalize_action = None  # type: ignore
        AuthorizationHook = None  # type: ignore
        risk_classify = None  # type: ignore
        MockToolExecutor = None  # type: ignore
        get_ledger = lambda: None  # type: ignore

# Policy engine for explicit deny demo
try:
    from policy_model.model import PolicyBundle, PolicyRule, PolicySource, PolicyDecision  # type: ignore
    from policy_engine.engine import PolicyEngine  # type: ignore
    from policy_engine.default_bundle import default_bundle  # type: ignore
except Exception:
    PolicyBundle = None  # type: ignore
    PolicyRule = None  # type: ignore
    PolicySource = None  # type: ignore
    PolicyDecision = None  # type: ignore
    PolicyEngine = None  # type: ignore
    default_bundle = None  # type: ignore


def _build_policy_engine(tenant_id: str = "default"):
    """Build PolicyEngine with default bundle + explicit deny for EXPORT (demo)."""
    if PolicyEngine is None or default_bundle is None:
        return None
    try:
        base = default_bundle(tenant_id)
        # Add explicit deny bundle for export — ensures EXPORT is always DENY (even for personal)
        # Section 25: Explicit Deny overrides Personal Delegation
        deny_export = PolicyBundle(
            id="demo-explicit-deny-export",
            tenant_id=tenant_id,
            name="Demo Explicit Deny — EXPORT",
            version="1.0.0",
            rules=[
                PolicyRule(
                    id="deny-export-gmail",
                    source=PolicySource.EXPLICIT_DENY,
                    action="EXPORT",
                    resource_pattern="gmail/*",
                    effect=PolicyDecision.DENY,
                    description="Demo: block gmail bulk export",
                ),
                PolicyRule(
                    id="deny-export-drive",
                    source=PolicySource.EXPLICIT_DENY,
                    action="EXPORT",
                    resource_pattern="drive/*",
                    effect=PolicyDecision.DENY,
                    description="Demo: block drive export",
                ),
                PolicyRule(
                    id="deny-export-all",
                    source=PolicySource.EXPLICIT_DENY,
                    action="EXPORT",
                    resource_pattern="*",
                    effect=PolicyDecision.DENY,
                    description="Demo: block all export",
                ),
            ],
        )
        # Demo extended bundle — allow all morning-briefing sources as LOW/MEDIUM (no token)
        demo_allow = PolicyBundle(
            id="demo-allow-briefing",
            tenant_id=tenant_id,
            name="Demo Allow — Morning Briefing (LOW/MEDIUM)",
            version="1.0.0",
            rules=[
                # Personal SEARCH extended: tasks, drive, calendar SEARCH all allowed via personal delegation
                PolicyRule(id="allow-personal-tasks-search", source=PolicySource.PERSONAL_DELEGATION, action="SEARCH", resource_pattern="tasks/user/*", effect=PolicyDecision.ALLOW),
                PolicyRule(id="allow-personal-tasks-read", source=PolicySource.PERSONAL_DELEGATION, action="READ", resource_pattern="tasks/user/*", effect=PolicyDecision.ALLOW),
                PolicyRule(id="allow-personal-drive-search", source=PolicySource.PERSONAL_DELEGATION, action="SEARCH", resource_pattern="drive/user/*", effect=PolicyDecision.ALLOW),
                PolicyRule(id="allow-personal-drive-read", source=PolicySource.PERSONAL_DELEGATION, action="READ", resource_pattern="drive/user/*", effect=PolicyDecision.ALLOW),
                PolicyRule(id="allow-personal-calendar-search", source=PolicySource.PERSONAL_DELEGATION, action="SEARCH", resource_pattern="calendar/user/*", effect=PolicyDecision.ALLOW),
                PolicyRule(id="allow-personal-gmail-search", source=PolicySource.PERSONAL_DELEGATION, action="SEARCH", resource_pattern="gmail/user/*", effect=PolicyDecision.ALLOW),
                # Enterprise / shared reads for briefing — default_bundle already allows outline READ, extend to SEARCH
                PolicyRule(id="allow-outline-search", source=PolicySource.DEFAULT_BUNDLE, action="SEARCH", resource_pattern="outline/*", effect=PolicyDecision.ALLOW),
                PolicyRule(id="allow-mattermost-search", source=PolicySource.DEFAULT_BUNDLE, action="SEARCH", resource_pattern="mattermost/*", effect=PolicyDecision.ALLOW),
                PolicyRule(id="allow-mattermost-read", source=PolicySource.DEFAULT_BUNDLE, action="READ", resource_pattern="mattermost/*", effect=PolicyDecision.ALLOW),
                PolicyRule(id="allow-crm-search", source=PolicySource.DEFAULT_BUNDLE, action="SEARCH", resource_pattern="crm/*", effect=PolicyDecision.ALLOW),
                PolicyRule(id="allow-crm-read", source=PolicySource.DEFAULT_BUNDLE, action="READ", resource_pattern="crm/*", effect=PolicyDecision.ALLOW),
            ],
        )
        # Merge: explicit deny first (highest priority), then briefing allows, then base
        return PolicyEngine([deny_export, demo_allow, base])
    except Exception:
        return None


async def _authorize_or_approval(
    hook: Any,
    ctx: dict,
    action: str,
    resource: str,
    tool_name: str,
) -> dict:
    """Run normalize + authz_hook + risk classify.

    Returns:
      {"decision": "ALLOW"|"DENY"|"APPROVAL_REQUIRED", "risk": "LOW|MEDIUM|HIGH", "reason": str, "allowed": bool}
    """
    # Normalize (will raise if invalid — treat as DENY)
    try:
        canon_action = canonicalize_action(action) if canonicalize_action else action.upper()
    except Exception as e:
        return {"decision": "DENY", "risk": "HIGH", "reason": f"invalid action: {e}", "allowed": False, "source": "validation"}
    try:
        canon_resource = normalize_resource(resource) if normalize_resource else resource
    except Exception as e:
        return {"decision": "DENY", "risk": "HIGH", "reason": f"invalid resource: {e}", "allowed": False, "source": "validation"}

    # Risk classification (deterministic, no token)
    risk_val = "LOW"
    if risk_classify:
        try:
            r = risk_classify(canon_action, canon_resource)
            risk_val = r.value if hasattr(r, "value") else str(r)
        except Exception:
            risk_val = "LOW"

    # Authz hook
    if hook is not None:
        try:
            result = await hook.authorize(ctx, action=canon_action, resource=canon_resource, tool_name=tool_name)
            return {
                "decision": result.decision,
                "reason": result.reason,
                "source": result.source,
                "risk": risk_val,
                "allowed": result.allowed,
                "matched_rule": result.matched_rule_id,
            }
        except Exception as e:
            return {"decision": "DENY", "risk": risk_val, "reason": f"authz error: {e}", "allowed": False, "source": "error"}
    # No hook — fallback allow personal, deny enterprise
    is_personal = is_personal_resource(canon_resource) if is_personal_resource else False
    if is_personal:
        return {"decision": "ALLOW", "risk": risk_val, "reason": "no hook fallback personal allow", "allowed": True, "source": "fallback"}
    return {"decision": "DENY", "risk": risk_val, "reason": "no hook fallback deny", "allowed": False, "source": "fallback"}


async def run_morning_briefing(agent_context: dict, tenant_id: str | None = None) -> dict:
    """Run morning briefing — aggregates 7 sources with real authz checks.

    Args:
        agent_context: dict with user_id, agent_id, tenant_id, session_id, trace_id, request_id
        tenant_id: override tenant

    Returns:
        dict with trace_id, briefing, sources, approvals_required, audit, decisions
    """
    tenant = tenant_id or agent_context.get("tenant_id", "default")
    trace_id = agent_context.get("trace_id") or f"trace_{uuid.uuid4().hex[:12]}"
    ctx = {**agent_context, "tenant_id": tenant, "trace_id": trace_id}
    if not ctx.get("request_id"):
        ctx["request_id"] = f"req_{uuid.uuid4().hex[:8]}"
    if not ctx.get("session_id"):
        ctx["session_id"] = f"sess_{uuid.uuid4().hex[:8]}"
    # Ensure agent_id derived if missing
    if not ctx.get("agent_id") and ctx.get("user_id", "").startswith("employee:"):
        ctx["agent_id"] = ctx["user_id"].replace("employee:", "agent:assistant:", 1)

    engine = _build_policy_engine(tenant)
    hook = AuthorizationHook(policy_engine=engine, tenant_id=tenant) if AuthorizationHook else None
    executor = MockToolExecutor(ctx) if MockToolExecutor else None  # type: ignore

    today_str = datetime.now(timezone.utc).astimezone().strftime("%Y년 %m월 %d일")

    # Tool plan — each maps to a mock executor method
    # calendar.list, gmail.search, tasks.list, drive.recent, outline.search, mattermost.mentions, crm.search
    plan: list[tuple[str, str, str, str, Any]] = []

    user_short = ctx.get("user_id", "").split(":")[-1] if ":" in ctx.get("user_id", "") else "unknown"

    plan.append(("calendar.list", "SEARCH", f"calendar/user/{user_short}/*", "calendar", lambda: executor.calendar_list() if executor else {"items": []}))
    plan.append(("gmail.search", "SEARCH", f"gmail/user/{user_short}/*", "gmail", lambda: executor.gmail_search(limit=10) if executor else {"items": []}))
    plan.append(("tasks.list", "SEARCH", f"tasks/user/{user_short}/*", "tasks", lambda: executor.tasks_list() if executor else {"items": []}))
    plan.append(("drive.recent", "SEARCH", f"drive/user/{user_short}/*", "drive", lambda: executor.drive_recent() if executor else {"items": []}))
    plan.append(("outline.search", "SEARCH", "outline/team/*", "outline", lambda: executor.outline_search() if executor else {"items": []}))
    plan.append(("mattermost.mentions", "SEARCH", "mattermost/channel/*", "mattermost", lambda: executor.mattermost_mentions() if executor else {"items": []}))
    plan.append(("crm.search", "SEARCH", "crm/customer/*", "crm", lambda: executor.crm_search() if executor else {"items": []}))

    sources: dict[str, Any] = {}
    decisions: dict[str, Any] = {}
    approvals_required: list[dict] = []

    for tool_name, action, resource, key, fn in plan:
        auth = await _authorize_or_approval(hook, ctx, action, resource, tool_name)
        decisions[tool_name] = auth
        if auth["decision"] == "APPROVAL_REQUIRED":
            approvals_required.append({"tool": tool_name, "action": action, "resource": resource, "reason": auth["reason"], "risk": auth["risk"]})
            sources[key] = {"status": "APPROVAL_REQUIRED", "reason": auth["reason"], "items": [], "count": 0, "trace_id": trace_id}
        elif not auth["allowed"]:
            sources[key] = {"status": "DENIED", "reason": auth["reason"], "items": [], "count": 0, "trace_id": trace_id}
        else:
            # Allowed — execute mock tool (LOW/MEDIUM)
            try:
                res = fn()
                # Ensure trace_id propagated
                if isinstance(res, dict) and "trace_id" not in res:
                    res["trace_id"] = trace_id
                sources[key] = {"status": "ok", "trace_id": res.get("trace_id", trace_id), **res}
            except Exception as e:
                sources[key] = {"status": "error", "reason": str(e), "items": [], "count": 0, "trace_id": trace_id}

    # High-risk EXPORT simulation — to demonstrate APPROVAL_REQUIRED / DENY
    # We intentionally check an EXPORT action but do NOT execute it (capability token 없이 HIGH는 APPROVAL_REQUIRED)
    export_resource = f"gmail/user/{user_short}/*"
    export_auth = await _authorize_or_approval(hook, ctx, "EXPORT", export_resource, "gmail.export")
    decisions["gmail.export"] = export_auth

    # Build Section 3.1 briefing summary
    cal_items = sources.get("calendar", {}).get("items", [])
    gmail_items = sources.get("gmail", {}).get("items", [])
    task_items = sources.get("tasks", {}).get("items", [])
    drive_items = sources.get("drive", {}).get("items", [])
    outline_items = sources.get("outline", {}).get("items", [])
    mattermost_items = sources.get("mattermost", {}).get("items", [])
    crm_items = sources.get("crm", {}).get("items", [])

    # Needs reply
    needs_reply = [m for m in gmail_items if m.get("needs_reply")]

    briefing_text = _format_briefing_text(today_str, cal_items, gmail_items, mattermost_items, task_items, drive_items, outline_items, crm_items)

    # Audit info
    ledger = get_ledger() if callable(get_ledger) else None
    audit_info = None
    if ledger is not None:
        try:
            audit_info = {
                "event_count": ledger.count,
                "chain_valid": ledger.verify_chain(),
                "head": ledger.head,
            }
        except Exception:
            audit_info = {"event_count": 0, "chain_valid": False}

    # Also include explicit export decision for tests
    export_status = export_auth["decision"]  # DENY (explicit) — proves Explicit Deny override

    result = {
        "trace_id": trace_id,
        "tenant_id": tenant,
        "user_id": ctx.get("user_id"),
        "agent_id": ctx.get("agent_id"),
        "date": today_str,
        "briefing": {
            "title": f"{today_str} 업무 브리핑",
            "summary_text": briefing_text,
            "sections": {
                "09:30 고객 A 미팅 준비": _build_meeting_section(cal_items, gmail_items, drive_items, crm_items),
                "11:00 개발회의": _build_dev_meeting_section(cal_items, mattermost_items),
                "오늘 반드시 처리": _build_todo_section(needs_reply, task_items),
            },
            "counts": {
                "calendar_today": len(cal_items),
                "emails_needing_reply": len(needs_reply),
                "mattermost_mentions": len(mattermost_items),
                "tasks_due_today": len([t for t in task_items if t.get("due") == "today"]),
                "drive_recent": len(drive_items),
                "outline_related": len(outline_items),
                "crm_today": len(crm_items),
            },
        },
        "sources": sources,
        "decisions": decisions,
        "approvals_required": approvals_required,
        "export_check": {"action": "EXPORT", "resource": export_resource, "decision": export_status, "reason": export_auth["reason"]},
        "audit": audit_info,
    }
    return result


def _format_briefing_text(today_str, cal, gmail, mm, tasks, drive, outline, crm) -> str:
    lines = [f"📋 {today_str} 업무 브리핑", ""]
    lines.append("📅 오늘 회의")
    for c in cal:
        lines.append(f"  - {c.get('start','')} {c.get('title','')} ({c.get('location','')})")
    lines.append("")
    lines.append(f"📧 회신 필요한 메일 {len([m for m in gmail if m.get('needs_reply')])}건 / 전체 {len(gmail)}건")
    for m in gmail[:3]:
        lines.append(f"  - [{m.get('from','')}] {m.get('subject','')}")
    lines.append("")
    lines.append(f"💬 Mattermost 멘션 {len(mm)}건")
    for item in mm:
        lines.append(f"  - [{item.get('channel','')}] {item.get('from','')}: {item.get('text','')}")
    lines.append("")
    lines.append(f"✅ 오늘 마감 작업 {len([t for t in tasks if t.get('due')=='today'])}건")
    for t in tasks:
        if t.get("due") == "today":
            lines.append(f"  - {t.get('title','')}")
    lines.append("")
    lines.append(f"📄 최근 문서 {len(drive)}건 / 📚 Outline {len(outline)}건")
    return "\n".join(lines)


def _build_meeting_section(cal, gmail, drive, crm):
    # 09:30 고객 A 미팅
    meeting = next((c for c in cal if "고객" in c.get("title","")), None)
    related_mails = [m for m in gmail if "A사" in m.get("subject","") or "customer-a" in m.get("from","")]
    latest_proposal = next((d for d in drive if "제안서" in d.get("name","")), None)
    crm_entry = next((c for c in crm if "고객 A" in c.get("customer","")), None)
    return {
        "meeting": meeting,
        "related_mails": related_mails[:3],
        "latest_proposal": latest_proposal,
        "crm": crm_entry,
    }


def _build_dev_meeting_section(cal, mm):
    dev_meeting = next((c for c in cal if "개발" in c.get("title","")), None)
    dev_mentions = [m for m in mm if m.get("channel") == "dev"]
    return {
        "meeting": dev_meeting,
        "mattermost_discussions": dev_mentions,
        "open_issues": 2,  # mock: 미처리 Issue 2건
    }


def _build_todo_section(needs_reply, tasks):
    todos = []
    for m in needs_reply:
        todos.append({"type": "email_reply", "subject": m.get("subject"), "from": m.get("from")})
    for t in tasks:
        if t.get("due") == "today":
            todos.append({"type": "task", "title": t.get("title")})
    return todos
