# Morning Briefing — Section 3.1

`오늘 내가 처리해야 할 업무 정리해줘` → Calendar+Email+Mattermost+Tasks+Drive+Outline+CRM 종합

## Architecture

```
User: "오늘 내가 처리해야 할 업무 정리해줘"
  ↓ Mattermost / Direct API
Control Plane (X-User-Id, tenant_id optional)
  ↓ AgentContext {tenant, user, agent, session, trace, request}
Orchestrator (examples/morning-briefing/orchestrator.py)
  ├─ normalize() + canonicalize_action() — resource/action canonicalization
  ├─ AuthorizationHook — personal owner isolation (GoogleConnector) / Outline ACL
  ├─ PolicyEngine — Explicit Deny > Personal Delegation > Default Bundle
  └─ risk.classify() — LOW/MEDIUM allow, HIGH → APPROVAL_REQUIRED (no token)
MockToolExecutor (execution-gateway/mock_executor.py)
  ├─ gmail.search / calendar.list / tasks.list / drive.recent / outline.search / mattermost.mentions / crm.search
  └─ trace_id propagation + AuditLedger (hash-chain)
Briefing JSON — 09:30 고객미팅 / 11:00 개발회의 / 오늘 반드시 처리
```

## Endpoints

### Control Plane

- `POST /v1/demo/morning-briefing` — headers `X-User-Id`, optional `X-Tenant-Id` or body `tenant_id`
  - No capability token: LOW/MEDIUM allow, HIGH returns `APPROVAL_REQUIRED`
  - Returns JSON briefing + `trace_id` (SSE 아님)
- `GET /v1/demo/health`

### Mattermost

- `POST /v1/mattermost/events` — if `text` contains `정리해줘`/`브리핑`/`업무 정리`, routes to same orchestrator and returns `routed: morning-briefing` with identical briefing payload.

## Authorization

- **Personal resources** (`gmail/user/kim/*`, `calendar/user/kim/*`, `drive/user/kim/*`, `tasks/user/kim/*`): owner isolation via `GoogleConnector.check_owner()` + PolicyEngine `PERSONAL_DELEGATION` (SEARCH/READ)
- **Shared** (`outline/*`, `mattermost/*`, `crm/*`): `OutlineConnector.check_acl()` + `DEFAULT_BUNDLE`
- **Explicit Deny** (`EXPORT` on `gmail/*`, `drive/*`, `*`) overrides personal — `Section 25` order verified

## Quickstart

```bash
PYTHONPATH=control-plane:execution-gateway:security/policy-engine:packages/policy-model:packages/audit-model:packages/common-types:examples/morning-briefing \
  python -m examples.morning_briefing.orchestrator  # standalone
# or via API
curl -H "X-User-Id: employee:kim" http://localhost:8000/v1/demo/morning-briefing
curl -X POST http://localhost:8000/v1/mattermost/events \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"employee:kim","text":"오늘 내가 처리해야 할 업무 정리해줘"}'
```

## Output Example

See [`output.json`](./output.json) — full briefing for `employee:kim` (trace_id propagated, 14 audit events, hash-chain valid).

```json
{
  "trace_id": "trace_...",
  "briefing": {
    "title": "2026년 08월 27일 업무 브리핑",
    "sections": {
      "09:30 고객 A 미팅 준비": {
        "meeting": {"title":"고객 A 미팅","start":"09:30"},
        "related_mails": [/* 3건 */],
        "latest_proposal": {"name":"A사_제안서_v3.pdf"},
        "crm": {"customer":"고객 A"}
      },
      "11:00 개발회의": {
        "meeting": {"title":"개발회의","start":"11:00"},
        "mattermost_discussions": [/* dev channel */],
        "open_issues": 2
      },
      "오늘 반드시 처리": [
        {"type":"email_reply","subject":"B사 견적 문의 — 회신 필요"},
        {"type":"task","title":"B사 회신"}
      ]
    },
    "counts": {"calendar_today":4,"emails_needing_reply":2,"mattermost_mentions":3}
  },
  "export_check": {"action":"EXPORT","decision":"DENY","reason":"matched deny-export-all @ explicit_deny"},
  "audit": {"event_count":14,"chain_valid":true}
}
```

## Tests

`tests/test_mvp_demo.py` — 5 tests (91 total with existing 86):
- kim 브리핑 성공 + Mattermost keyword parity
- trace 유지
- Audit 체인 검증
- cross-user isolation (kim vs lee)
- Explicit Deny (export) 차단
