# Open Agent OS v0.1.1 — Personal AX Business Platform

> **Self-Hosted Enterprise Personal Agent OS** — One Personal Agent per Employee, bridging personal and enterprise work securely — Source-Available (BSL 1.1)

**한국어** | [English](README.md)

<p align="center">
  <img src="assets/oaos-logo.jpg" alt="OAOS 로고" width="220" />
</p>

<h1 align="center">OAOS</h1>
<p align="center"><strong>사람과 지식, 그리고 AI가 연결되는 곳.</strong></p>

- **브랜드:** OAOS
- **Repository:** `openit-ai/open-agent-os`
- **기준 아키텍처:** [`docs/architecture-v1.7.0.md`](docs/architecture-v1.7.0.md) — 5026 lines, SHA `2ebeb981`

---

## 1. 제품 정의

**Open Agent OS (OAOS)** 는 직원 한 명당 **하나의 Logical Personal Agent(개인 디지털 업무 Identity)** 를 제공하는 **설치형 Enterprise Personal Agent OS**다. 개인 업무 맥락과 기업 공동 맥락을 안전하게 연결한다.

- **개인 영역(내가 위임):** Email, Calendar, Drive, Tasks, SaaS, Memory — Personal Delegation으로 연결하며 관리자 승인 없이 내가 직접 위임한다.
- **기업 영역(정책+JIT 승인으로 통제):** Mattermost / Slack, Outline / Notion, ERP / CRM / GitHub, 사내 시스템 — Policy Engine이 결정하며 LLM이 allow/deny를 판단하지 않는다.
- **설치형:** 고객사 서버 / VPS / 전용 클라우드 / K8s에 설치한다. 멀티테넌트 SaaS 종속과 데이터 외부 이전 없이 고객 인프라에 데이터가 남는다.

OAOS는 Mattermost·Slack·Outline·Notion·Hermes를 대체하지 않는다. 이들을 Personal Agent 중심으로 **안전하게 연결하는 Control + Security + Execution 플랫폼**이다.

**왜 이 구조인가 — 시장이 풀지 못한 두 가지 모순(§2):**

1. *공용 Agent에 개인 정보를 맡길 수 없다.* 공용 `company-agent`는 누구의 Gmail·Drive인지 답할 수 없고, credential을 한 곳에 모으면 노출·cross-user leakage·memory 오염이 발생한다.
2. *개인 도구에 닿지 못하는 Agent는 매일 쓸 이유가 없다.* 메일·일정·문서·할 일·기억에 닿지 못하면 가끔 쓰는 검색봇으로 남고, 사용빈도·위임·자동화·AX 효과가 모두 낮아진다.

**OAOS의 답:** `개인 업무환경(Personal Delegation) ↔ Logical Personal Agent ↔ 기업 공동환경(Policy + 승인)` — Agent는 일상의 실행 주체가 된다: `파악 → 정리 → 탐색 → 조율 → 실행 → 승인 요청 → 연계 → 기억`(§1, §14).

## 2. Personal Wiki

**Personal Wiki**는 직원별 Personal Agent에 연결된 개인 지식공간이다. **Mattermost / Slack Web·Desktop·Mobile에서 ACP를 통해 동일한 Personal Agent**로 접근하며, 휴대폰에서 시작한 업무를 태블릿·PC에서 자연스럽게 이어서 수행한다.

```text
Mattermost / Slack Web·Desktop·Mobile
                ↓ ACP
       Employee Personal Agent
                ↓
 Personal Wiki = Vault + Memory + Search
```

- 대화·문서·회의·일정·업무 결과를 시간의 흐름에 따라 축적한다.
- `tenant_id` / `agent_id` 기반으로 직원별 업무 맥락을 구조화한다.
- OAOS 서버 Vault와 Memory Service가 원본·검색·출처를 연결한다.
- 축적된 지식을 아침 브리핑·문서 탐색·업무 조율·후속 실행에 재활용한다.

구현: `§27B Vault` — `/var/lib/oaos/vault/{tenant}/{agent:assistant:*}/{journal,notes,projects,files,attachments}` 레이아웃, 첨부 추출(pdf/docx/xlsx/pptx/image OCR), Execution Gateway를 통한 모든 tool 결과 자동 아카이빙, `memory_service`의 owner-isolated 검색(pgvector 1536 + TF-IDF fallback). 상세: [`docs/personal-wiki-design.md`](docs/personal-wiki-design.md).

## 3. 전사 지식 인덱스 & ACL-aware Hybrid RAG

전사 위키와 기업 문서는 **ACL-aware Hybrid RAG**로 검색한다. Outline·Notion 등 원 시스템은 문서 원본과 현재 권한을 유지하고, OAOS는 `oaos` PostgreSQL + pgvector에 검색용 **Knowledge Index(전사 지식 인덱스)** 를 저장한다.

```text
Outline / Notion (원본 + 현재 ACL)
        ↓ Connector / MCP — 청킹·임베딩·source reference·content hash·ACL version
 oaos PostgreSQL + pgvector
        ├─ lexical index
        ├─ semantic vector index
        ├─ tenant / group / agent 메타데이터
        ├─ source reference (source_resource_id, source_uri, content_hash, source_updated_at)
        └─ ACL provenance (acl_version)
        ↓ 검증된 Agent Context
 ACL pre-filter → lexical + semantic retrieval → 중복 제거 & 재정렬 → source reference → Personal Agent 응답
```

**Knowledge Index 저장 필드:** `index_id, source_system, source_resource_id, source_uri, tenant_id, group_id/agent_id, chunk_id, chunk_text, embedding (pgvector 1536), content_hash, source_updated_at, indexed_at, acl_version, classification, retention_policy, provenance` — Index는 검색을 빠르게 하기 위한 파생 인덱스이며, 원본 시스템이 소스 오브 트루스다. 연결은 `source_resource_id`·`source_uri`·`content_hash`·`source_updated_at`·`acl_version`으로 추적한다.

**검색 순서:**

1. 검증된 Agent Context에서 `tenant_id` / 사용자 / 그룹 / Agent 범위를 확정한다.
2. source ACL과 `acl_version`을 기준으로 검색 후보 범위를 만든다.
3. PostgreSQL lexical 검색과 pgvector semantic 검색을 병행한다.
4. 후보를 중복 제거하고 metadata·최신성·문서 유형을 반영해 재정렬한다.
5. 답변에 사용할 결과의 source reference와 provenance를 연결한다.
6. 권한 변경 또는 원본 변경이 감지되면 해당 Index를 갱신하거나 재검증한다.

**핵심 불변식:** ACL은 결과를 만든 뒤 걸러내는 방식이 아니라 **검색 후보를 만들기 전에 적용**한다. Personal Wiki 검색은 `agent_id` 중심, 전사 검색은 원 시스템의 사용자·그룹·collection ACL과 `tenant_id` 중심이다. 결과에는 항상 제목·소스 시스템·URL·수정 시각·provenance를 함께 제공해 원문 확인과 업무 실행으로 바로 연결한다.

**업무 연속성:** Mattermost와 Slack의 Web·Desktop·Mobile에서 동일한 Personal Agent를 사용하며, 질문에 따라 Personal Wiki만, 전사 Knowledge Index만, 혹은 둘을 결합해 답한다.

```text
"내가 지난주 처리한 업무"         → Personal Wiki + 업무 도구
"회사 정책과 관련 문서"            → 전사 Knowledge Index
"내 업무와 회사 정책을 함께 비교"  → Personal Wiki + Knowledge Index
```

## 4. 아키텍처 흐름

```text
Mattermost / Slack ──► Control Plane (Identity / Session / ACP) ──► Hermes Runtime (Security-Domain Worker Pool)
                                  │  Internal Agent API                  │ Tool / MCP
                                  └──────► Execution Gateway (MCP Registry / Risk / AuthZ / Proxy) ──► Personal (Google) / Shared (Outline) / Enterprise (CRM/ERP)
                                                              ▲
                                              Security (Delegation / Vault / Policy / Token / Approval / Audit)
Admin Console (Next.js + shadcn) ──► Security API 프록시 ──────┘  users / policy / approvals / audit / credentials / infra

Personal Wiki (Vault FS + memory_service pgvector) ◄── Execution Gateway auto-archive (every tool call, trace_id)
전사 Knowledge Index (Postgres + pgvector) ◄── Connectors (Outline/Notion) — ACL versioned
```

**불변식:** `Personal Delegation(내 자원은 내가 위임) ↔ Enterprise Authorization(회사 자원은 정책+승인)`, `Explicit Deny > Personal`, `Agent Permission ≤ User Permission`, `Cross-user 항상 DENY`, `Auditable(hash-chain + HMAC checkpoint)`.

**런타임:** LLM Runtime이 canonical(`llm`, `safe`는 deprecated alias)이고 Hermes Runtime이 advanced — Registry YAML(LLM Only / Hermes Only / Both), Router 5-step, Capability `EXECUTE runtime/*`, untrusted worker(§16G), tool policy(§16H), data access(§16I). 상세: `docs/architecture-v1.7.0.md` §§16A–16K, §§16.1.1–16.1.2, §§16.4–16.8.

## 5. 핵심 가치 5가지

1. **Personal-First, Enterprise-Safe** — 내 Calendar/Gmail은 내가 위임(§9), Production/ERP/고객DB는 회사 정책+승인(§11). UX와 보안이 함께 자연스럽다(§13).
2. **진짜 격리** — `agent:assistant:kim`은 `employee:kim` 소유 자원만 본다. Cross-user는 항상 DENY, plaintext token 저장 금지, Hermes 프로세스에 장기 저장 금지(§10).
3. **사람이 승인하는 고위험 실행** — `MERGE/DEPLOY/PAY/EXPORT` 등 HIGH risk(§21)는 Capability Token(HS256 300s, nonce/jti replay 방지) + HMAC 승인 요청(§24) + Admin Console 4버튼(Deny / Once / Always사용자 / Always그룹)으로만 실행한다.
4. **감사 가능한 운영** — 모든 권한·위임·실행은 Audit Ledger에 hash-chain+HMAC checkpoint로 기록되며 변조가 즉시 탐지된다(§30-31). `verify_chain` / `checkpoint` API를 제공한다.
5. **설치형 Source-Available** — BSL 1.1(4년 후 Apache 2.0 전환), 고객 인프라에 설치하며 설치·모니터링·백업·지원 옵션을 제공한다(§5).

## 6. Quick Start

```bash
git clone https://github.com/openit-ai/open-agent-os.git && cd open-agent-os

# 1) Python (3.11) — 전체 테스트
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"  # 또는 pip install -r requirements.txt
pytest -q
```

```bash
# 2) Admin Console
cd admin-console && npm install && npm run build   # static routes
OAOS_ENV=development ADMIN_JWT_SECRET=dev-only-change-me npm run dev  # :3000 — bootstrap login via ADMIN console
  # Dev/test only (OAOS_ENV != production): seeded L5 admin via bootstrap.
  # Production: OAOS_ADMIN_BOOTSTRAP_PASSWORD 필수 (절대 커밋·로그 금지) — 미설정 시 fail-closed.
```

```bash
# 3) Backend (별도 터미널, repo root에서)
uvicorn security.app:app --port 8001 --reload
uvicorn admin_console.backend.app:app --port 8002 --reload
uvicorn control_plane.app:app --port 8000 --reload
```

```bash
# 4) MVP Demo (Hermes 없이)
curl -X POST http://localhost:8000/v1/demo/morning-briefing \
  -H "X-User-Id: employee:kim" -H "X-Tenant-Id: default" | jq
# → briefing(summary, today_meetings 4, emails 7, trace_id, audit_events 14)

curl -X POST http://localhost:8000/v1/mattermost/events \
  -H "Content-Type: application/json" \
  -d '{"text":"정리해줘","user_id":"employee:kim"}' | jq  # 키워드 라우팅
```

### Docker (평가용)

```bash
cp .env.example .env   # OAOS_SIGNING_KEY 등
docker compose -f deploy/docker-compose.dev.yml up -d
# services: control-plane(:8000) execution-gateway(:8001) admin-api(:8002) admin-console(:3000) postgres redis
```

## 7. 배포

| Edition | 내용 |
|---|---|
| Developer | 소스 접근, 기본 Personal Agent, 로컬 평가 |
| Business | + Vault / JIT Approval / Audit / Admin Console / IAM |
| Managed | Business + 고객 소유 인프라 설치·모니터링·백업 |

설치형: 고객사 서버 / VPS / 전용 클라우드 / K8s — 멀티테넌트 SaaS가 아니다.

**Compose & K8s:**

- `deploy/docker-compose.dev.yml`은 평가용, `deploy/docker-compose.prod.yml`은 healthcheck(`curl -f /healthz`, interval 30s) + `restart: unless-stopped` + nginx 게이팅(`depends_on: service_healthy`).
- `deploy/k8s/`는 `replicas: 2`, `RollingUpdate(maxUnavailable:1,maxSurge:1)`, `livenessProbe`는 `/healthz`(30s), `readinessProbe`는 `/readyz`(10s), `podAntiAffinity(hostname)`, `PodDisruptionBudget(minAvailable:1)`, `HPA`(2–10, CPU 70% / mem 80%). 상세: [`docs/ha.md`](docs/ha.md), [`docs/deployment.md`](docs/deployment.md).
- **Health 엔드포인트:** `GET /healthz`(liveness), `GET /readyz`(readiness, bounded DB/Redis 체크), `GET /v1/health/detailed`(상세). Production에서는 의존성 장애 시 `/readyz`가 `503`을 반환해 트래픽에서 제외된다.

다음 단계: KVM4 단일 VPS에 Mattermost + Outline + Hermes + OAOS를 `deploy/docker-compose.prod.yml`로 통합하는 것이 로드맵이다.

## 8. 보안 경계

- **Policy Engine(§25):** fnmatch glob, strict eval, Explicit Deny가 Personal Delegation을 override한다.
- **Delegation:** fingerprint + binding cascade revoke가 즉시 반영된다.
- **Vault:** Fernet AES+HMAC(`OAOS_VAULT_KEY`/`VAULT_ENCRYPTION_KEY` sha256→b64 derive, `vault://admin_llm_providers/{id}/api_key`, `encrypted_api_key=gAAAAA…`, `****` 마스킹), owner `agent:assistant:<user>` 격리, DB-backed + fail-soft fallback.
- **Token:** HS256 300s short-lived + nonce/jti replay store.
- **Approval:** HMAC-SHA256, 4 decisions(`DENIED / APPROVED_ONCE / APPROVED_USER_ALWAYS / APPROVED_GROUP_ALWAYS`), nonce/signature/expiry.
- **Audit:** hash-chain + HMAC checkpoint(`verify_chain`, `checkpoint`).
- **격리 보장:** `agent:assistant:kim`은 `employee:lee` 자원에 접근할 수 없다. `test_delegation_isolation`, `test_cross_user_session_isolation 403`, `test_app_policy_evaluate_explicit_deny`, `test_audit_verify_chain+tamper`로 검증한다.
- **네트워크 & worker 격리(§§16A, 16G–16I):** Hermes는 untrusted worker(`hermes` uid, `/home/hermes` 샌드박스, `nftables` + Controlled Egress Proxy — ACP:8000, MCP:8001, LLM Gateway, 승인된 package mirror만 허용; Production DB·ERP/CRM·SSH·다른 사용자 홈·vault secret 직접 접근은 DENY). 상세: [`docs/security-model.md`](docs/security-model.md), `deploy/firewall/hermes-egress.nft`.

Admin Console 화면(11+ routes: login / dashboard / infra / users / policy / approvals / audit / credentials / providers)은 shadcn + WCAG AA, `overflow-auto`로 375px 대응, `npm run build`로 검증된다. Infra 화면은 `GET /v1/infra/health`를 병렬 probe(3s timeout, 15s polling)하며 쓰기는 L5, 읽기는 L4가 필요하다.

## 9. 검증 근거

로컬에서 커밋 단위로 직접 재현한다 — 외부 검증 주장이 필요하지 않다:

```bash
pytest -q
# 기대값 (2026-08-29, main): 813 passed, 1 skipped, 74 warnings — LLM 6-Provider, Fernet Vault,
# opencode 바이너리 체인, wiki/pgvector, production hardening(fail-closed runtime/deploy/audit/approval/token/rate + secrets) 포함.
# 필터 예시:
pytest tests/test_workstream_a.py tests/test_control_plane_api.py -v  # isolation / SSE
pytest tests/test_workstream_b.py -v       # cross-user deny / HIGH token / trace
pytest tests/test_workstream_c.py -v       # policy / audit / vault / approval
pytest tests/test_mvp_demo.py -v           # kim vs lee isolation, EXPORT deny
pytest tests/test_admin_backend.py -v      # register / login / JWT / bcrypt / RBAC / health mock
```

- **이 README는 체크아웃한 커밋에서 직접 측정한 `pytest -q` 결과를 보고한다.** 고정 보장이 아니므로 변경 후에는 다시 실행해 확인한다.
- **Liveness / readiness (production fail-closed):** `GET /healthz`는 항상 `200`, `GET /readyz`는 production에서 DB/Redis 체크 실패 시 `503`을 반환해 pod을 트래픽에서 제외한다(non-prod에서는 `checks` 상세를 담은 `200 degraded`일 수 있음). `SIGTERM` 드레이닝 중에도 `terminationGracePeriodSeconds: 30` 동안 `503 draining`으로 트래픽을 차단한다. K8s는 `readinessProbe` 실패 시 Endpoints에서 제외한다.
- **Audit chain:** `verify_chain`으로 변조를 탐지하고 `checkpoint`는 HMAC으로 서명된다.

> **Historical note (참고용):** `docs/architecture-v1.7.0.md`(`2ebeb981`, 5026 lines)는 `648 tests` 포함한 production hardening(fail-closed runtime/deploy/audit/approval/token/rate + secrets) 시점에 기록되었다. `v1.6.4` 시점은 `612`, 그 이전 마일스톤은 `590` / `180`이었다. 이 수치는 특정 시점의 스냅샷이며 현재 결과를 대체하지 않는다.

## 10. Repository Structure

```text
packages/                  # Phase 0 contracts (Pydantic 불변): agent-context, policy-model, audit-model, delegation-model, mcp-resource-model, common-types
control-plane/             # Identity(derive_agent_id 1:1), Session(assert_owner→403), Router(HIGH→ephemeral), ACP(X-Agent-Context, SSE), Internal API(§17), Demo
execution-gateway/         # normalize(domain/scope/path), mcp_registry(wildcard·역색인), connectors(google check_owner/outline ACL), risk(§21), authz_hook, proxy(trace, HIGH token 필수), mock_executor, wiki_archive 훅
security/                  # policy-engine(§25), delegation(fingerprint·cascade revoke), vault(Fernet), token(HS256 300s·nonce/jti), approval(HMAC), audit(hash-chain·checkpoint), app(FastAPI)
admin-console/             # Next.js 15 + shadcn Financial(#22C55E/#F59E0B/#DC2626), 375px, routes: login/dashboard/infra/users/policy/approvals/audit/credentials/providers + backend(auth L5/L4 JWT·infra CRUD·health probe·policy/approval/audit/credentials 프록시)
adapters/                  # Mattermost/Slack/Outline/Notion/Hermes/IAM/Google/Microsoft
packages/personal-wiki/    # Personal Wiki Vault FS(journal/notes/projects/files/attachments), extractor, consolidate, memory_service client
examples/morning-briefing/ # MVP — orchestrator(per-user kim vs lee) + output.json(13KB) + README
deploy/                    # docker-compose.dev/prod.yml + k8s (Section 32) + firewall(hermes-egress.nft)
tests/                     # 검증 근거 참조 — 현재 수치는 pytest -q로 확인
docs/architecture-v1.7.0.md  # Canonical (47 Sections + §§16A–16K + §16.1.1–16.1.2 LLM 6-Provider + §§16.4–16.6 Quota/Usage/HA + §27B Wiki Vault + §§16.7–16.8 Production Hardening — SHA 2ebeb981)
```

## 11. Docs

- [`docs/architecture-v1.7.0.md`](docs/architecture-v1.7.0.md) — Canonical (47 Sections + §§16A–16K + §16.1.1–16.1.2 LLM 6-Provider + §§16.4–16.6 Quota/Usage/HA + §27B Wiki Vault + §§16.7–16.8 Production Hardening). Previous: [`docs/architecture-v1.6.4.md`](docs/architecture-v1.6.4.md) `e10c1af8`(historical), `docs/architecture-v1.1.md` preserved.
- [`docs/architecture-v1.7.1-design.md`](docs/architecture-v1.7.1-design.md) — Critical/High hardening 설계(C1/H1–H8, Personal Wiki JWT, 전사 Knowledge Index spec, readiness strict, 분산 상태).
- [`docs/personal-wiki-design.md`](docs/personal-wiki-design.md) — Personal Wiki Vault / extractor / consolidation / memory_service 연동.
- [`docs/security-model.md`](docs/security-model.md) — Dual runtime, untrusted worker, tool policy, data access, egress allowlist.
- [`docs/ha.md`](docs/ha.md) + [`docs/deployment.md`](docs/deployment.md) — HA probes, PDB, HPA, zero-downtime 절차.
- `docs/api/` — Internal Agent Interface, Capability, Approval API.
- `examples/morning-briefing/README.md` — MVP 브리핑 형식(`09:30 / 11:00 / 오늘 반드시 처리`).

## 12. License

**Business Source License 1.1** — see [`LICENSE`](./LICENSE).

- **Licensor:** OpenIT Co., Ltd. / **Licensed Work:** Open Agent OS
- **Additional Use Grant:** Developer Edition 평가·개발·테스트 목적의 production 외 사용 허용, 그 외 production/호스팅/재배포는 Business/Managed 별도 상업 라이선스 필요
- **Change Date:** `2030-08-27`(v0.1.1 기준, 이후 버전은 각 릴리즈일로부터 4년) — **Change License:** Apache 2.0 자동 전환
- BSL 원문: https://mariadb.com/bsl11/ · Apache 2.0: https://www.apache.org/licenses/LICENSE-2.0
