# Open Agent OS v1.1

> **설치형 Enterprise Personal Agent Platform** — 1 user = 1 Logical Personal Agent, Personal Delegation과 Enterprise Authorization을 분리해 개인·기업 업무를 안전하게 연결하는 Source-Available 플랫폼

- **Repository:** `openit-ai/open-agent-os`
- **Architecture:** `docs/architecture-v1.1.md` (Sections 1–47) — Control Plane / Execution Gateway / Security & Governance 3분할
- **Status:** `122d9e13` — Workstream A+B+C + MVP Demo + Admin Console (11 routes) 완료, `108 tests pass`, `npm run build ✓`

## 30초 요약

```
Mattermost/Slack ──► Control Plane (Identity/Session/ACP) ──► Hermes Runtime (Security-Domain Worker Pool)
                                  │ Internal Agent API               │ Tool/MCP
                                  └──────► Execution Gateway (MCP Registry / Risk / AuthZ / Proxy) ──► Personal(Google) / Shared(Outline) / Enterprise(CRM/ERP)
                                                              ▲
                                              Security (Delegation/Vault/Policy/Token/Approval/Audit)
Admin Console (Next.js + shadcn) ──► Security API 프록시 ──────┘  users/policy/approvals/audit/credentials/infra
```

핵심 불변식: **Personal Delegation(내 자원은 내가 위임) ↔ Enterprise Authorization(회사 자원은 정책+승인), Explicit Deny > Personal, Agent Permission ≤ User Permission, Cross-user 항상 DENY, Auditable(hash-chain)**

## Repository Structure

```
packages/                  # Phase 0 Contracts (Pydantic 불변): agent-context, policy-model, audit-model, delegation-model, mcp-resource-model, common-types
control-plane/             # A — Identity(derive_agent_id 1:1), Session(assert_owner→403), Router(HIGH→ephemeral), ACP(X-Agent-Context, SSE), Internal API(§17), Demo(morning briefing)
execution-gateway/         # B — normalize(domain/scope/path), mcp_registry(wildcard·역색인), connectors(google check_owner/outline ACL), risk(§21), authz_hook, proxy(trace, HIGH token 필수), mock_executor(7 tools/14 audit)
security/                  # C — policy-engine(§25 Strict, fnmatch, Explicit Deny>Personal), delegation(fingerprint·cascade revoke), vault(Fernet), token(HS256 300s·nonce/jti replay), approval(HMAC 4 decisions), audit(hash-chain·checkpoint), app(FastAPI)
admin-console/             # Admin — Next.js 15 + shadcn Financial(#22C55E/#F59E0B/#DC2626), 375px, 11 routes: login/dashboard/infra/users/policy/approvals/audit/credentials + backend(auth L5/L4 JWT·infra CRUD·health probe·policy/approval/audit/credentials 프록시)
adapters/                  # Mattermost/Slack/Outline/Notion/Hermes/IAM/Google/Microsoft
examples/morning-briefing/ # MVP — orchestrator(per-user kim vs lee) + output.json(13KB) + README
deploy/                    # docker-compose.dev/prod.yml + k8s (Section 32)
tests/                     # 108 tests: control-plane 5 + A 7 + B 35 + C 33 + admin 17 + MVP 5 + e2e 6
docs/architecture-v1.1.md  # Truth (47 Sections)
```

## Quick Start

```bash
git clone https://github.com/openit-ai/open-agent-os.git && cd open-agent-os

# 1) Python (3.11) — 전체 테스트
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"  # 또는 pip install -r requirements.txt
pytest -q                # 108 passed (12.8s)

# 2) Admin Console
cd admin-console && npm install && npm run build   # 11 routes, 114–115kB
NEXT_PUBLIC_API_URL=http://localhost:8002 npm run dev  # :3000 — login admin@openit.co.kr / Admin123!

# 3) Backend (별도 터미널, repo root에서)
uvicorn security.app:app --port 8001 --reload
uvicorn admin_console.backend.app:app --port 8002 --reload
# Control Plane: uvicorn control_plane.app:app --port 8000 --reload

# 4) MVP Demo (Hermes 없이)
curl -X POST http://localhost:8000/v1/demo/morning-briefing -H "X-User-Id: employee:kim" -H "X-Tenant-Id: default" | jq
# → briefing(summary, today_meetings 4, emails 7, trace_id, audit_events 14)
curl -X POST http://localhost:8000/v1/mattermost/events -H "Content-Type: application/json" \
  -d '{"text":"정리해줘","user_id":"employee:kim"}' | jq  # 키워드 라우팅
```

### Docker (평가용)

```bash
cp .env.example .env   # OAOS_SIGNING_KEY 등
docker compose -f deploy/docker-compose.dev.yml up -d
# services: control-plane(:8000) execution-gateway(:8001) admin-api(:8002) admin-console(:3000) postgres redis
```

## Admin Console (Section 22 + 23-24·30-31)

| 화면 | 설명 |
|---|---|
| Login | `admin@openit.co.kr / Admin123!` → JWT(HS256 8h) localStorage |
| Infra | 서비스 등록(host/port/health_path) + `healthy/unhealthy/unknown` 배지 + `GET /v1/infra/health` 병렬 probe(3s) + 15s 폴링, 쓰기 L5/읽기 L4 |
| Users | 이메일/display_name/role/생성일, L5 전용 등록·삭제, 자기삭제 400 차단 |
| Policy | Bundle(tenant/version/rules) + Rule 테이블(source/action·resource glob, decision ALLOW/DENY/APPROVAL_REQUIRED, priority §25), Explicit Deny 빨강 |
| Approvals | 대기 큐(approval_id/user/agent/action/resource/risk HIGH/MEDIUM/LOW/만료) + 4버튼(Once / Always사용자 / Always그룹 / Deny) |
| Audit | 타임라인 역순(event_type/user/agent/resource/decision, hash/prev_hash) + Verify(chain_valid/checkpoint_valid) + checkpoint 카드 |
| Credentials | Provider별 active/revoked/expired + 최근 위임 10 |

모든 화면 shadcn + WCAG AA, `overflow-auto`로 375px 대응, `npm run build` 11 routes static.

## Security Model

- **Policy Engine(§25):** fnmatch glob, Strict eval, Explicit Deny가 Personal Delegation override
- **Delegation:** fingerprint + binding cascade revoke 즉시 적용
- **Vault:** Fernet AES+HMAC, owner `agent:assistant:<user>` 격리, `EncryptedPostgresVault` stub→prod 전환 예정
- **Token:** HS256 300s short-lived + nonce/jti replay store
- **Approval:** HMAC-SHA256, 4 decisions(`DENIED/APPROVED_ONCE/APPROVED_USER_ALWAYS/APPROVED_GROUP_ALWAYS`), nonce/signature/expiry
- **Audit:** hash-chain + HMAC checkpoint(`verify_chain`, `checkpoint`)
- **Isolation 검증:** `test_delegation_isolation`, `test_cross_user_session_isolation 403`, `test_app_policy_evaluate_explicit_deny`, `test_audit_verify_chain+tamper` 등 108 tests

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

| Edition | 내용 |
|---|---|
| Developer | 소스 접근, 기본 Personal Agent, 로컬 평가 |
| Business | + Vault/JIT Approval/Audit/Admin Console/IAM |
| Managed | Business + 고객 소유 인프라 설치·모니터링·백업 |

설치형: 고객사 서버/VPS/전용 클라우드/K8s — 멀티테넌트 SaaS 아님. Next: KVM4 단일 VPS에 Mattermost+Outline+Hermes+O-AOS 통합(`deploy/docker-compose.prod.yml`).

## Docs

- `docs/architecture-v1.1.md` — Truth (47 Sections: §3.1/36 morning briefing, §5 설치형, §7.2 Gateway, §21 risk, §25 policy, §40 보안 테스트)
- `docs/api/` — Internal Agent Interface, Capability, Approval API
- `examples/morning-briefing/README.md` — MVP 브리핑 형식(09:30/11:00/오늘 반드시 처리)

## License

**Business Source License 1.1** — see [`LICENSE`](./LICENSE).

- **Licensor:** OpenIT Co., Ltd. / **Licensed Work:** Open Agent OS
- **Additional Use Grant:** Developer Edition 평가·개발·테스트 목적의 production 외 사용 허용, 그 외 production/호스팅/재배포는 Business/Managed 별도 상업 라이선스 필요
- **Change Date:** `2030-08-27` (v1.1 기준, 이후 버전은 각 릴리즈일로부터 4년) — **Change License:** Apache 2.0 자동 전환
- BSL 원문: https://mariadb.com/bsl11/ · Apache 2.0: https://www.apache.org/licenses/LICENSE-2.0

