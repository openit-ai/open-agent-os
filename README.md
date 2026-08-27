# Open Agent OS v0.1.1 — Personal AX Business Platform

> **Self-Hosted Enterprise Personal Agent OS** — One Personal Agent per Employee, bridging personal and enterprise work securely — Source-Available (BSL 1.1)

[한국어](README.ko.md) | **English**

- **Repository:** `openit-ai/open-agent-os`
- **Architecture:** `docs/architecture-v1.1.md` (Sections 1–47) — Control Plane / Execution Gateway / Security & Governance
- **Status:** `v0.1.1` — Workstream A+B+C + MVP Demo + Admin Console (11 routes), `108 tests pass`, `npm run build ✓`

## Why Open Agent OS — Two Contradictions the Market Hasn't Solved

**Contradiction 1. You can't entrust personal data to a shared agent.**

> "Who would connect their Gmail / Calendar / Drive / Tasks to a shared company agent?"

A shared enterprise agent (`company-agent`) has no structural answer — it is unclear *whose* Gmail it is and *who* owns the credential. Centralizing credentials creates exposure, cross-user leakage, memory contamination, and complex revocation. Useful for knowledge search, but structurally unfit as a Personal Assistant that handles daily work. (§2.1)

**Contradiction 2. An agent that can't reach personal tools has no reason to be used daily.**

An agent without access to mail, calendar, documents, tasks, meetings, and memory becomes "an occasional search bot" — low usage → low delegation → low automation → no AX transformation. (§2.2)

**Open Agent OS answer: One Logical Personal Agent per Employee**

```
Personal Work Environment (Email / Calendar / Drive / Tasks / SaaS / Memory)
        +  Personal Delegation (my resources, delegated by me — no admin approval)
        ↕  Logical Personal Agent — Employee's Digital Work Identity
        +  Enterprise Authorization (company resources — policy + JIT approval)
Enterprise Shared Environment (Mattermost / Slack / Outline / ERP / CRM / GitHub)
```

Beyond Q&A, the agent becomes a **daily work executor** — `discover → organize → search → coordinate → execute → request approval → integrate → remember`. (§1, §14)

## Market Analysis

| Dimension | Existing Enterprise AI | Open Agent OS |
|---|---|---|
| **Target** | Large enterprise, cloud SaaS | **SME AX transformation** — self-hosted, extensible to public / healthcare / manufacturing (§5) |
| **Deployment** | Multi-tenant SaaS (data lock-in) | **Customer server / VPS / Private Cloud / K8s** — data ownership, environment isolation, security review, minimal exit lock-in |
| **Agent model** | One shared agent + shared credentials | **Per-employee Logical Personal Agent** + Hermes Security-Domain Worker Pool (General / Dev / Finance / Admin / High-risk ephemeral) (§16) |
| **Authorization** | Single RBAC, LLM decides allow/deny | **Personal Delegation ↔ Enterprise Policy separated** — every decision by Policy Engine (§25, not LLM), `Agent Permission ≤ User Permission` |
| **Trust** | Prompt-based, weak audit | **Least privilege · Human approval · Auditable (hash-chain)** — JIT Approval in 4 levels (Deny / Once / Always for user / Always for group) (§12, §23) |

**Positioning:** Does not replace Mattermost, Slack, Outline, Notion, or Hermes — it is the **Control + Security + Execution Platform that securely connects them around the Personal Agent**. (§4)

**Demand proof — Morning Briefing (§3.1):** One message in Mattermost — "Summarize what I need to handle today" — aggregates 4 calendar events + 7 emails needing reply + 3 mentions + 2 deadlines + Drive / Outline / CRM into a `09:30 Client Meeting / 11:00 Dev Meeting / Must-do today` briefing. Only possible with simultaneous access to personal and enterprise context.

## 5 Core Values

1. **Personal-First, Enterprise-Safe** — Calendar / Gmail delegated by me (§9); Production / ERP / customer DB governed by company policy + approval (§11). Natural UX and security at once. (§13)
2. **True isolation** — `agent:assistant:kim` sees only `employee:kim`-owned resources. Cross-user always DENY, no plaintext token storage, no long-term storage in Hermes process (§10). Verified by 108 tests.
3. **Human-approved high-risk execution** — HIGH-risk (§21) actions such as `MERGE / DEPLOY / PAY / EXPORT` run only via Capability Token (HS256, 300s, nonce/jti replay protection) + HMAC approval request (§24) + 4-button Admin Console decision.
4. **Auditable operations** — Every authorization, delegation, and execution is recorded in the Audit Ledger as a hash-chain with HMAC checkpoint — tampering is immediately detectable (§30–31). `verify_chain` / `checkpoint` APIs.
5. **Self-Hosted, Source-Available** — BSL 1.1 (converts to Apache 2.0 after 4 years), deploy on customer infrastructure. Evaluate (Developer) → operate (Business / Managed) without SaaS lock-in. (§5, Editions)

## Architecture at a Glance

```
Mattermost/Slack ──► Control Plane (Identity/Session/ACP) ──► Hermes Runtime (Security-Domain Worker Pool)
                                  │ Internal Agent API               │ Tool/MCP
                                  └──────► Execution Gateway (MCP Registry / Risk / AuthZ / Proxy) ──► Personal(Google) / Shared(Outline) / Enterprise(CRM/ERP)
                                                              ▲
                                              Security (Delegation/Vault/Policy/Token/Approval/Audit)
Admin Console (Next.js + shadcn) ──► Security API proxy ──────┘  users/policy/approvals/audit/credentials/infra
```

Core invariant: **Personal Delegation (my resources, delegated by me) ↔ Enterprise Authorization (company resources — policy + approval), Explicit Deny > Personal, Agent Permission ≤ User Permission, Cross-user always DENY, Auditable (hash-chain)**

## Repository Structure

```
packages/                  # Phase 0 Contracts (Pydantic, immutable): agent-context, policy-model, audit-model, delegation-model, mcp-resource-model, common-types
control-plane/             # A — Identity (derive_agent_id 1:1), Session (assert_owner→403), Router (HIGH→ephemeral), ACP (X-Agent-Context, SSE), Internal API (§17), Demo (morning briefing)
execution-gateway/         # B — normalize (domain/scope/path), mcp_registry (wildcard reverse-index), connectors (google check_owner / outline ACL), risk (§21), authz_hook, proxy (trace, HIGH requires token), mock_executor (7 tools / 14 audits)
security/                  # C — policy-engine (§25 Strict, fnmatch, Explicit Deny > Personal), delegation (fingerprint + cascade revoke), vault (Fernet), token (HS256 300s + nonce/jti replay), approval (HMAC, 4 decisions), audit (hash-chain + checkpoint), app (FastAPI)
admin-console/             # Admin — Next.js 15 + shadcn Financial (#22C55E/#F59E0B/#DC2626), 375px, 11 routes: login/dashboard/infra/users/policy/approvals/audit/credentials + backend (auth L5/L4 JWT, infra CRUD, health probe, policy/approval/audit/credentials proxy)
adapters/                  # Mattermost / Slack / Outline / Notion / Hermes / IAM / Google / Microsoft
examples/morning-briefing/ # MVP — orchestrator (per-user kim vs lee) + output.json (13KB) + README
deploy/                    # docker-compose.dev/prod.yml + k8s (Section 32)
tests/                     # 108 tests: control-plane 5 + A 7 + B 35 + C 33 + admin 17 + MVP 5 + e2e 6
docs/architecture-v1.1.md  # Source of truth (47 Sections)
```

## Quick Start

```bash
git clone https://github.com/openit-ai/open-agent-os.git && cd open-agent-os

# 1) Python 3.11 — run all tests
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"  # or pip install -r requirements.txt
pytest -q                # 108 passed (12.8s)

# 2) Admin Console
cd admin-console && npm install && npm run build   # 11 routes, 114–115kB
NEXT_PUBLIC_API_URL=http://localhost:8002 npm run dev  # :3000 — login admin@openit.co.kr / Admin123!

# 3) Backend (separate terminals, from repo root)
uvicorn security.app:app --port 8001 --reload
uvicorn admin_console.backend.app:app --port 8002 --reload
# Control Plane: uvicorn control_plane.app:app --port 8000 --reload

# 4) MVP Demo (without Hermes)
curl -X POST http://localhost:8000/v1/demo/morning-briefing -H "X-User-Id: employee:kim" -H "X-Tenant-Id: default" | jq
# → briefing (summary, today_meetings 4, emails 7, trace_id, audit_events 14)
curl -X POST http://localhost:8000/v1/mattermost/events -H "Content-Type: application/json" \
  -d '{"text":"summarize","user_id":"employee:kim"}' | jq  # keyword routing
```

### Docker (evaluation)

```bash
cp .env.example .env   # set OAOS_SIGNING_KEY etc.
docker compose -f deploy/docker-compose.dev.yml up -d
# services: control-plane(:8000) execution-gateway(:8001) admin-api(:8002) admin-console(:3000) postgres redis
```

## Admin Console (Section 22 + 23–24, 30–31)

| Screen | Description |
|---|---|
| Login | `admin@openit.co.kr / Admin123!` → JWT (HS256, 8h) in localStorage |
| Infra | Service registry (host/port/health_path) + `healthy/unhealthy/unknown` badge + `GET /v1/infra/health` parallel probe (3s) + 15s polling, write L5 / read L4 |
| Users | Email / display_name / role / created_at, L5-only create/delete, self-delete blocked (400) |
| Policy | Bundle (tenant/version/rules) + Rule table (source/action·resource glob, decision ALLOW/DENY/APPROVAL_REQUIRED, priority §25), Explicit Deny in red |
| Approvals | Pending queue (approval_id/user/agent/action/resource/risk HIGH/MEDIUM/LOW/expiry) + 4 buttons (Once / Always for user / Always for group / Deny) |
| Audit | Reverse-chronological timeline (event_type/user/agent/resource/decision, hash/prev_hash) + Verify (chain_valid/checkpoint_valid) + checkpoint card |
| Credentials | Per-provider active/revoked/expired + last 10 delegations |

All screens: shadcn + WCAG AA, `overflow-auto` for 375px, `npm run build` 11 static routes.

## Security Model

- **Policy Engine (§25):** fnmatch glob, Strict evaluation, Explicit Deny overrides Personal Delegation
- **Delegation:** fingerprint + binding cascade revoke, immediate effect
- **Vault:** Fernet AES+HMAC, owner `agent:assistant:<user>` isolation, `EncryptedPostgresVault` stub → prod
- **Token:** HS256 300s short-lived + nonce/jti replay store
- **Approval:** HMAC-SHA256, 4 decisions (`DENIED / APPROVED_ONCE / APPROVED_USER_ALWAYS / APPROVED_GROUP_ALWAYS`), nonce/signature/expiry
- **Audit:** hash-chain + HMAC checkpoint (`verify_chain`, `checkpoint`)
- **Isolation verified:** `test_delegation_isolation`, `test_cross_user_session_isolation 403`, `test_app_policy_evaluate_explicit_deny`, `test_audit_verify_chain+tamper` — 108 tests

## Tests

```bash
pytest -q                          # 108 passed
pytest tests/test_workstream_a.py tests/test_control_plane_api.py -v  # 12 (A isolation/SSE)
pytest tests/test_workstream_b.py -v  # 35 (cross-user deny/HIGH token/trace)
pytest tests/test_workstream_c.py -v  # 33 (policy/audit/vault/approval)
pytest tests/test_mvp_demo.py -v      # 5 (kim vs lee isolation, EXPORT deny)
pytest tests/test_admin_backend.py -v # 17 (register/login/JWT/bcrypt/RBAC/health mock)
```

## Editions & Deployment

| Edition | Includes |
|---|---|
| Developer | Source access, base Personal Agent, local evaluation |
| Business | + Vault / JIT Approval / Audit / Admin Console / IAM |
| Managed | Business + installation / monitoring / backup on customer-owned infra |

Self-hosted on customer server / VPS / private cloud / K8s — not multi-tenant SaaS. Next: single KVM4 VPS integrating Mattermost + Outline + Hermes + O-AOS (`deploy/docker-compose.prod.yml`).

## Docs

- `docs/architecture-v1.1.md` — Source of truth (47 Sections: §3.1/36 morning briefing, §5 self-hosted, §7.2 Gateway, §21 risk, §25 policy, §40 security tests)
- `docs/api/` — Internal Agent Interface, Capability, Approval APIs
- `examples/morning-briefing/README.md` — MVP briefing format (09:30 / 11:00 / Must-do today)

## License

**Business Source License 1.1** — see [`LICENSE`](./LICENSE).

- **Licensor:** OpenIT Co., Ltd. / **Licensed Work:** Open Agent OS
- **Additional Use Grant:** Non-production use for Developer Edition evaluation / development / testing; production / hosting / redistribution requires separate Business/Managed commercial license
- **Change Date:** `2030-08-27` (for v0.1.1; each later version 4 years after its release) — **Change License:** Apache 2.0
- BSL text: https://mariadb.com/bsl11/ · Apache 2.0: https://www.apache.org/licenses/LICENSE-2.0
