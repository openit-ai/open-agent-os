# Open Agent OS v0.1.10 — Personal AX Business Platform

> **Self-Hosted Enterprise Personal Agent OS** — One Personal Agent per Employee, bridging personal and enterprise work securely — Source-Available (BSL 1.1)

**한국어** | [English](README.md)

<p align="center">
  <img src="assets/oaos-logo.jpg" alt="OAOS 로고" width="220" />
</p>

<h1 align="center">OAOS</h1>
<p align="center"><strong>사람과 지식, 그리고 AI가 연결되는 곳.</strong></p>

- **브랜드:** OAOS
- **Repository:** `openit-ai/open-agent-os`
- **제품 버전:** `0.1.10` — 단일 진실 `admin-console/package.json` `0.1.10` (직전 제품 태그 `v0.1.9` → `2079bb8`; 0.1.10 릴리스 내용은 테스트/import/store 격리 PR #29(`569f8a5`), oaos.cloud 기본값 PR #30(`ac20c76`), live registry 격리 PR #31(`5d7b89e`)을 포함; 그 이전 제품 태그 `v0.1.8` → `78c9a29`). **아키텍처 문서 버전 `v1.7.5`(`docs/architecture-v1.7.5.md`)는 제품 버전 `0.1.10`과 별개** — v1.7.5는 v1.7.4 이후 병합 완료 내역(§16.16)을 기록하며 제품 릴리즈 번호를 의미하지 않는다.
- **기준 아키텍처:** [`docs/architecture-v1.7.5.md`](docs/architecture-v1.7.5.md) — v1.7.5 v1.7.4 이후 완료 기록(§16.16) + Admin Console IA 전환 완료(§16.15) + Control-Plane 중심 IA 별칭(§16.14)
- **사용자 등록:** [`OAOS 사용자 등록 표준 가이드 v1.0`](docs/oaos-user-registration-guide-v1.0.md) — Mattermost 계정 확인, 인사말·호칭·최초 성향 파악, 세션 분리, 선택적 Google Workspace OAuth 절차

---

## 1. 제품 정의

**Open Agent OS (OAOS)** 는 직원 한 명당 **하나의 Logical Personal Agent(개인 디지털 업무 Identity)** 를 제공하는 **설치형 Enterprise Personal Agent OS**다. 개인 업무 맥락과 기업 공동 맥락을 안전하게 연결한다.

- **개인 영역(내가 위임):** Email, Calendar, Drive, Tasks, SaaS, Memory — Personal Delegation으로 연결하며 관리자 승인 없이 내가 직접 위임한다.
- **기업 영역(정책+JIT 승인으로 통제):** Mattermost / Slack, Outline / Notion, ERP / CRM / GitHub, 사내 시스템 — Policy Engine이 결정하며 LLM이 allow/deny를 판단하지 않는다.
- **설치형:** 고객사 서버 / VPS / 전용 클라우드 / K8s에 설치한다. 멀티테넌트 SaaS 종속과 데이터 외부 이전 없이 고객 인프라에 데이터가 남는다.

OAOS는 Mattermost·Slack·Outline·Notion·Hermes를 대체하지 않는다. 이들을 Personal Agent 중심으로 **안전하게 연결하는 Control + Security + Execution 플랫폼**이다.

Admin Console은 연결·설정·실행·지식·모니터링·관리의 6개 내비게이션 그룹과 그룹별 canonical URL을 사용한다. 단순 legacy URL 17개는 query를 보존하는 임시 `307` 별칭으로 유지하고, fragment 의존 `/infra`와 `/providers`는 클라이언트 resolver를 사용한다. 공통 UX 킷은 표·대화상자·확인·토스트·폼·로딩·빈 상태·오류·상태·URL query 흐름을 제공한다.

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
Admin Console (Next.js + shadcn) ──► Admin API ──► Control Plane / Execution Gateway / Security
                                    6그룹: 연결 / 설정 / 실행 / 지식 / 모니터링 / 관리

Personal Wiki (Vault FS + memory_service pgvector) ◄── Execution Gateway auto-archive (every tool call, trace_id)
전사 Knowledge Index (Postgres + pgvector) ◄── Connectors (Outline/Notion) — ACL versioned
```

**불변식:** `Personal Delegation(내 자원은 내가 위임) ↔ Enterprise Authorization(회사 자원은 정책+승인)`, `Explicit Deny > Personal`, `Agent Permission ≤ User Permission`, `Cross-user 항상 DENY`, `Auditable(hash-chain + HMAC checkpoint)`.

**런타임:** LLM Runtime이 canonical(`llm`, `safe`는 deprecated alias)이고 Hermes Runtime이 advanced — Registry YAML(LLM Only / Hermes Only / Both), Router 5-step, Capability `EXECUTE runtime/*`, untrusted worker(§16G), tool policy(§16H), data access(§16I). 상세: `docs/architecture-v1.7.2.md` §§16A–16K, §§16.1.1–16.1.2, §§16.4–16.8.

### 런타임 소유권과 Mattermost 세션 격리 (v1.7.2)

OAOS는 사용자의 직접 Hermes/Telegram 세션을 재사용하지 않는다. Mattermost 경로는 사용자별 소유권으로 분리하고, OAOS 운영 Redis 세션 저장소를 거친 뒤 Hermes Gateway API를 호출한다. Telegram의 `/model` override(예: `custom/gpt-5.6-luna`)는 직접 Hermes 세션의 설정이며 OAOS가 상속해서는 안 된다.

```text
Mattermost mykim → oaos-mm-bridge → OAOS Control Plane :8100
                 → Redis 세션(`oaos:mattermost:<tenant>:<verified-user>`)
                 → Hermes Gateway API :8642
```

2026-08-30 검증: OAOS 서비스와 health/readiness endpoint는 active/HTTP 200, Control Plane 운영 환경은 `OAOS_SESSION_BACKEND=redis`와 `OAOS_CP_HERMES_BASE_URL=http://127.0.0.1:8642`, 브리지는 `state.db`/`sessions.json` 직접 참조 없음, 대상 OAOS 회귀 테스트는 `117 passed`였다. 이는 구조·프로세스 증거이다. 실제 `mykim` source post `jazr64zt6p8m8qendcpqnnur1h`를 `u5yq38w4d3gii8zdi48r6p39zw` 채널에서 read-back했고, 동일 thread(`root_id=jazr64zt6p8m8qendcpqnnur1h`)의 이후 봇 응답 `c8gawge517837j3o3zoo44swgc`를 확인했다. 관찰된 사용자 Mattermost 외부 E2E는 **통과**했다. 별도 probe post `xjmo488frbdafnkwwutft49qeh`는 브리지 봇 작성 글이어서 정상적으로 skip됐다.

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

Admin Console 내비게이션은 고정 2항목과 연결·설정·실행·지식·모니터링·관리 6그룹의 canonical 링크 30개로 구성되며, 데스크톱 사이드바와 모바일 drawer는 `AdminNavigation`으로 같은 계층을 공유한다. 단순 legacy 경로 17개는 query를 보존하는 임시 서버 `307` redirect를 사용하고 `/infra`와 `/providers`는 fragment 인식 client resolver를 유지한다. 공통 UX 킷은 표·대화상자·확인·토스트·폼·로딩/빈 상태/오류/상태·URL query 처리를 표준화하며 쓰기는 L5, 읽기는 L4가 필요하다.

## 9. 검증 근거

로컬에서 커밋 단위로 직접 재현한다 — 외부 검증 주장이 필요하지 않다:

```bash
pytest -q
# 과거 스냅샷 (2026-08-29, main): 927 passed, 1 skipped, 74 warnings — 현재 v0.1.3 증거가 아님. 현재 후보 증거는 docs/deployment-verification-v0.1.3.md 및 docs/evidence-report-v0.1.3.json에 기록. LLM 6-Provider, Fernet Vault,
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

### 9a. 릴리즈 v0.1.3 — 제품 `0.1.3` (아키텍처 `v1.7.2`와 별개)

**태그/커밋:** `v0.1.3` 후보 `6d91f3b710` on `release/v0.1.3-remediation` (미태깅; 이전 `v0.1.2`는 `34f0981e71` on `origin/main`). 제품 버전 `0.1.3`는 `admin-console/package.json`에서 읽는다(env `OAOS_VERSION`이 우선); 아키텍처 `v1.7.2`는 설계 문서 버전으로 별개 — 혼동 금지.

**이번 태그 포함 내역 (11 files, 647 insertions):**
- **Admin Web UI 수정**
  - `admin-console/lib/api.ts`: `BASE_URL` `http://localhost:8000` → `"/api"` same-origin (nginx `/api` 프록시), 401 `handleUnauthorized` 토큰/아바타 제거 → `/login` replace (login 경로 루프 방지), `formatApiErrorDetail`은 `[object Object]`를 절대 출력하지 않음 (array/object/message/msg/error/detail/reason/title → JSON fallback), `normalizeLLMUsage*` + `updateMapping`/`display_name`·`avatar_url` 확장.
  - `admin-console/app/(dashboard)/llm-usage/page.tsx`: 관대한 `formatNumber`/`formatCost`/`formatTime`, `safeNum`, 방어적 `items` 정규화 (`timestamp`/`created_at`, `tenant`/`tenant_id`, `total_tokens` fallback), `total = total ?? count ?? length`.
  - `admin-console/backend/llm_providers.py`: `_admin_usage_history`에 `tenant`/`timestamp` 별칭 + `total` 응답 추가, `_admin_usage_summary`에 `daily_tokens`/`daily_quota`/`daily_usage_ratio`/`per_minute_tokens`/`per_minute_limit`/`success_rate`/`p50`/`p99`/`hourly_*`/`updated_at` 확장 (기존 키 유지, 하위 호환).
- **Version UI**
  - `admin-console/components/VersionDisplay.tsx` (신규): `GET /version` (4s abort, no-store)로 설치 버전 조회, `t("header.version")` `Open Agent OS v{version}` 치환, `updateAvailable=false|null`이면 설치 버전만 표시, 아니면 `v0.1.3 -> vX.Y.Z`로 최신을 빨강으로 표시.
  - `admin-console/app/version/route.ts` (신규, primary) + `admin-console/app/api/version/route.ts` (호환): `getInstalledVersion()` env/package.json/fallback `0.1.3`, `normalizeTag`/`compareSemver`, `fetchLatestGithubVersion()` → `GET /repos/openit-ai/open-agent-os/releases/latest` → fallback `GET /tags`, 3s bounded, `Cache-Control: public, s-maxage=3600, stale-while-revalidate=600`.
  - `admin-console/app/(dashboard)/layout.tsx`: footer `VersionDisplay`가 정적 텍스트 대체.
  - `admin-console/next.config.js` + `admin-console/lib/i18n/en.json,ko.json`: `version: "Open Agent OS v{version}"` 플레이스홀더 치환, `latestAvailable`, env 전파.
- **P0 / P1 (이미 main에 포함, HEAD에 존재 확인 — 재커밋 아님)**
  - P0 `00a6fcb890` — Mattermost durable idempotency (Redis `tenant+channel+post` 결정적 키, 원자적 claim, completed/failed_retryable, prod 503, bounded retry) + active Agent Runtime 경유 multimodal 이미지 전달 (ACP/Hermes).
  - P1 `57e9a4fcc2` — live Knowledge Index 검증 (health `check_*_credentials`/`probe_*_health`, ACL pre-filter 계약, `content_hash`/`acl_version` 증분 동기화, bounded retry/checkpoint, `scripts/verify-knowledge-live.py`).
  - P1 fix `8855441b83` — clean checkout용 fail-closed Notion adapter 처리 (`http_notion.py` 부재 → 결정적 blocker, crash 없음, P1 24 tests 통과).

**Knowledge Index / RAG 경계 (과장 없는 정직한 서술):**
- **Outline — read-only 검증만.** 커넥터 `HttpOutlineSourceAdapter`는 read-only, bounded 단일 페이지 probe (`page_limit=1..5`), `source_reference`/`content_hash`/`acl_version` 추적. 분산/외부 전체 RAG는 **이번 릴리즈에서 주장하지 않는다**. 라이브 corpus probe는 bounded이며, 원본 저장소가 source of truth이고 전체 backfill/분산 검증은 라이브 credential + `scripts/verify-knowledge-live.py --health`가 필요하다 — P1 범위 `§0.4, §16.9-16.11` 참조. 관측된 바에 따르면 bounded paging probe는 `page_limit=1`에서 최대 약 869 페이지까지 보고한다(`health.py` 코드 주석), 이는 대량 ingestion을 주장하지 않고 corpus 규모를 나타낸다.
- **Notion — 이번 릴리즈에서 live 연동 미완료.** `HttpNotionSourceAdapter`(`http_notion.py`)는 read-only이며 fail-closed: 모듈 부재 또는 credential 부재(`NOTION_API_KEY`/`OAOS_NOTION_TOKEN`) 시 결정적 blocker `Notion adapter missing: knowledge_index/connectors/http_notion.py not present` / `Notion credentials missing ... live Notion connector not verifiable`, `verifiable=false`, `adapter_missing=true` (해당 시), mock fallback이나 가짜 health 없음. Live Notion 검증은 `http_notion.py` + credential + network가 필요하다 — 완료로 주장하지 않는다.
- **Distributed/external은 0으로 유지** — 라이브 검증(`kind`+`redis`+`hubble`+`Outline/Notion/Mattermost/Slack/LLM gateway` 라이브 네트워크)까지 — `scripts/verify-evidence-tiers.py` tiers 참조. `scripts/verify-knowledge-live.py`가 커넥터 운영 검증 수단이며, `scripts/verify-evidence-tiers.py --check-only`가 지원되지 않는 주장에 대해 H8을 강제한다.

### 9b. 릴리즈 v0.1.4 — 제품 `0.1.4` (아키텍처 `v1.7.2`와 별개)

**포함 내역 (v0.1.3 대비 관리자 콘솔 설정면, additive-only):**
- **Setup/ACP/MCP/MM/Outline** (`/v1/setup`, `/v1/acp`, `/v1/mcp`, `/v1/mattermost`, `/v1/outline` + infra 탭) — 최초실행 마법사, effective 자동탐지, 마스킹 점검, 서비스 테이블 자동등록.
- **Runtime Config plane (P0)** — `/v1/runtime/config` 스냅샷/발행/롤백/상태/감사 + 통합 infra `/live`·`/registry`·`/seed` 탭 (main 병합, 기존 CRUD 유지).
- **커넥터 (P1)** — `/v1/notion`, `/v1/slack`, `/v1/oauth`, `/v1/smtp` (키 write-only, MM/Outline 독립).
- **운영면 (P2/P3)** — `/quota`, `/embedding`, `/secrets`(메타만, 로테이션 실행 없음), `/feature-flags`, `/profile-ops`, `/knowledge-ops`(상태 + 큐 투입 + dry-run, 확인토큰 리셋).

**측정 근거 (2026-09-04, 병합 커밋):** 전수 `1358 passed, 5 skipped, 6 failed` — 6건은 clean main에서도 동일 재현(기존 순서 의존, worktree 검증); 표적 admin `39 passed`; `npm run build` 성공(25+ 페이지). 원격 배포 검증: systemd 4종 active, 신규 API 30종, 신규 페이지 HTTP 200, CP/EGW/Hermes/MM/Outline 헬스 정상, alembic head `018`, Hermes/MM/Outline 코드 경로 무수정.

> **Historical note (참고용):** `docs/architecture-v1.7.2.md`(`2ebeb981`, 5026 lines)는 `648 tests` 포함한 production hardening(fail-closed runtime/deploy/audit/approval/token/rate + secrets) 시점에 기록되었다. `v1.6.4` 시점은 `612`, 그 이전 마일스톤은 `590` / `180`이었다. 이 수치는 특정 시점의 스냅샷이며 현재 결과를 대체하지 않는다.

### 9c. 릴리즈 v0.1.5 — 제품 `0.1.5` (아키텍처 `v1.7.3`와 별개)

**포함 내역 (v0.1.4 대비 — 통합 + Control-Plane 중심 IA, additive-only):**
- **Google OAuth + MM 첨부** — Control Plane 소유 PKCE/state 콜백, 사용자별 Vault 바인딩, 읽기전용 Google API 경로; 브리지가 MM 첨부를 owner-scoped Wiki Vault로 스트리밍(500MiB 캡·SHA-256·민감명 마스킹).
- **Outline 지식 + Mattermost RAG** — Outline→Knowledge Index bounded sync(tenant/source 체크포인트); MM→CP 경로 tenant/agent/group ACL 선행필터 retriever + provenance.
- **Control-Plane 중심 IA 별칭 (§16.14)** — 어드민 `/control/acp`·`/control/runtime`·`/execution/mcp` 뷰; 백엔드 `/v1/control/acp/*`(3)·`/v1/execution/mcp/*`(5) 별칭(정본 endpoint 공유); 정식 유닛 `oaos-adapter-mattermost`·`oaos-governance`(구 유닛 1릴리스 병행); 스냅샷 `process_aliases` 참조 전용.

**측정 근거 (2026-09-05, main `b49073112a` + 본 범프):** 전수 `1525 passed, 4 skipped, 10 failed` — 9건은 전수 순서에서만 실패(clean worktree `b49073112a`·본 트리 격리 실행 모두 통과), 1건(`test_stage5_migration_backup::test_existing_table_preservation`)은 clean worktree 재현(기존 결함); 표적 admin+runtime-config `28 passed`; 로컬·KVM4 운영 `npm run build` 성공(신규 3라우트); 별칭 import 8건 + 운영 live HTTP(정본/별칭 `401` 인증게이트, console/api/CP `200`); KVM4 선별 sync(서버 편차 19건 보존) 및 백업; `oaos-admin-api`·`oaos-admin-console` 재기동, 5종 active.

### 9d. 릴리즈 v0.1.6 — 제품 `0.1.6` (아키텍처 `v1.7.3`와 별개)

**포함 내역 (v0.1.5 대비 — 안정화 백포트 + 린트 baseline, additive-only):**
- **Vault fail-closed 강화** — hvac-optional 경로 실제 인증 상태 보고; HTTP transport 실패 시 fallback 전 중단; unreachable-probe 테스트 격리; tenant mock quota 강제 및 실패 usage 기록.
- **테스트 격리 수정** — admin loader/SQLite/engine-cache 격리, IAM/LLM/runtime-config/workstream teardown + 명시적 주입, migration-head 기대치 갱신.
- **Ruff baseline 확정** — `docs/ruff-baseline.json`(3416건 보존, current-vs-baseline 신규 0건); 병합 후 delta 재검증 `신규 0 / 해소 0`.

**측정 근거 (2026-09-09, main `7c1c0ef` PR #7 병합):** 표적 회귀 `106 passed`; `test_stage5_migration_backup.py` 호스트 러너 `6 passed`(샌드박스 subprocess 시간 초과 2건은 환경 문제로 입증, fail-closed); compile PASS; `git diff --check` PASS; 상용 서버 `v0.1.5` 헬스 정상(5종 active, 콘솔 `200` + 설치 `0.1.5`).

### 9e. 릴리즈 v0.1.7 — 제품 `0.1.7` (아키텍처 `v1.7.4`와 별개)

**포함 내역 (v0.1.6 대비 — Admin Console IA 전환 완료):**
- **6그룹 canonical IA(§16.15)** — 연결·설정·실행·지식·모니터링·관리의 canonical 내비게이션 링크 30개와 데스크톱 사이드바·모바일 drawer의 동일 `AdminNavigation` 계층.
- **호환 정책** — 단순 legacy URL 17개는 query를 보존하는 임시 서버 `307` 별칭, fragment 의존 `/infra`와 `/providers`는 client resolver 유지, legacy client page는 롤백 경로로 보존.
- **공통 Admin UX 킷** — 표/query, 대화상자/확인, 토스트, 폼, loading/empty/error/status, 접근성, i18n, `useSearchParams()`/`Suspense` 계약 표준화.

### 9f. 릴리즈 v0.1.8 — 제품 `0.1.8` (아키텍처 `v1.7.4`와 별개)

**포함 내역 (v0.1.7 대비 — Admin Console 마감 작업):**
- **setup 마법사 완주(PR #20)** — 주기 점검이 부팅 시 기동하고 종료 시 정리됩니다. 관찰한 점검 결과가 300초 메모리 TTL이 아니라 DB에 영속됩니다. canonical 인프라 레지스트리가 idempotent하게 시드됩니다. `applied` 판정이 `source == "env"`가 아니라 `config_revision == effective_revision`입니다. 각 차단 사유가 사람이 읽는 안내(ko/en)로 렌더되고 진단 코드는 보조 텍스트로 남습니다.
- **canonical 시드 opt-out·Outline 헬스(PR #21)** — `OAOS_INFRA_SEED_EXCLUDE`가 운영자가 지운 행을 3개 재시드 경로(기동·`/seed`·콘솔 목록) 모두에서 영구 제외합니다. Outline 실시간 점검은 `/_health`를 쓰는데, `GET /`가 포트 없는 `https://127.0.0.1/`로 `301`을 반환해 리다이렉트로는 `200`에 도달할 수 없었습니다.
- **설정 3화면 한글화(PR #22)** — policy·approvals·audit가 모든 라벨을 카탈로그로 렌더합니다. 직접 원인은 키 부재가 아니라 `ko.json`에 방치된 영어 값(예: `approvals.requester = "Requester"`)이었고, 3화면에 인라인으로 박혀 있던 영문도 `t()` 조회로 전환했습니다.
- **설정 3화면 구조 정렬(PR #23)** — 3화면 본문을 `components/admin/control/control-features.tsx`로 이관하고 라우트를 11줄 `Suspense` 래퍼로 축소해 다른 그룹과 동일한 구조로 맞췄습니다. 이관으로 이 코드가 `components/admin/**` eslint 범위에 들어오며 기존 `exhaustive-deps` 경고 2건이 드러났고, 파생 배열을 memoize해 해소했습니다.
- **카탈로그 정리·누락 키 가드(PR #24)** — 아무 파일도 참조하지 않는 275키를 삭제했고(1,335 → 1,060, ko/en 정합 유지), 카탈로그 테스트가 이제 화면이 조회하는 리터럴 키나 동적 접두사를 카탈로그가 해석하지 못하면 **빌드를 실패**시킵니다.

**측정 근거 (main `e512244`, PR #23 + #24 병합):** `tsc --noEmit` 0; `vitest run` **31 files / 116 tests passed**; `eslint .` 0; `next build` 0(**56/56** 정적 페이지); 카탈로그 **1,060 = 1,060**. 가드는 실효를 실증했습니다 — `t("admin.probe.missingKey")`를 추가하면 그 키를 지목하며 스위트가 실패하고, 제거하면 다시 통과합니다.

### 9g. 릴리즈 v0.1.9 — 제품 `0.1.9` (아키텍처 `v1.7.4`와 별개)

**포함 내역 (v0.1.8 대비 — 감사 원장 무결성):**
- **감사 체인이 더 이상 분기하지 않습니다(PR #27)** — `AuditLedger.append`가 프로세스 인메모리 head에서 이어져, 다른 appender가 쓴 뒤 생성된 인스턴스가 낡은 부모를 사용해 두 이벤트가 같은 부모를 공유할 수 있었습니다. 상용에 분기 27개가 있었습니다. 이제 head를 저장된 체인(아무도 링크하지 않는 이벤트)에서 읽고, 모듈 수준 락 + 트랜잭션 스코프 PostgreSQL advisory lock으로 append를 직렬화해 프로세스 간에도 같은 부모에서 갈라지지 않습니다. 개별 이벤트의 해시는 전부 일치했으므로 **변조는 없고 연결 구조만** 깨져 있었습니다.
- **정책 변경과 감사 기록이 한 트랜잭션입니다(PR #27)** — `publish`/`approve`/`rollback`이 상태 변경을 커밋한 뒤 감사를 기록해서, 감사 원장 실패 시 `500`을 반환하면서도 변경이 적용된 채 남았습니다(상용에서 `1.0.0`이 감사 기록 없이 게시됨). 이제 변이 함수가 감사 콜백을 받아 같은 트랜잭션에서 실행하므로, 감사 실패 시 변경도 롤백됩니다.

**측정 근거 (PR #27, 병합 main `351ee4b`):** 신규 테스트 9건이 **이전 구현에서 실패, 수정 후 통과**; 관련 9개 스위트 **147 passed**; ruff 신규 발견 **0**(`ledger.py` 17→17, `policy.py` 40→40 — 기존 baseline 유지).

**운영 주의:** `AuditLedger.head`/`events`는 여전히 인메모리 값을 우선하므로, 디스크에서 이력을 변경한 뒤에는 장수명 프로세스의 **재시작이 필요**합니다.

### 9h. 릴리즈 v0.1.10 — 제품 `0.1.10` (아키텍처 `v1.7.5`와 별개)

**포함 내역 (v0.1.9 대비 — 결정적 테스트, oaos.cloud 기본값, 레지스트리 격리):**
- **패키지 경유 import와 테스트 상태 격리(PR #29)** — 정책 원자성 테스트가 `backend.policy`를 패키지 경유로 import하여 경로 순서가 잘못된 `auth` 모듈을 해석하지 못하게 했습니다. 모듈 사이에서 import-time 환경 변경을 복원하고, Outline/infra 커버리지가 실제 배포 도메인 `note.oaos.cloud`와 `chat.oaos.cloud`를 사용하며, 로드된 모든 admin infra store를 초기화하고 EOF의 불필요한 빈 줄을 제거했습니다.
- **Outline/Mattermost 기본값 oaos.cloud 전환(PR #30)** — 백엔드 기본값, UI fixture, Outline 테스트 fixture, Mattermost bridge 기본값을 `note.oaos.cloud`와 `chat.oaos.cloud`로 바꿨습니다. 저장소 검색에서 `note.openit.co.kr` 잔존 **0건**, `chat.openit.co.kr` 잔존 **0건**을 실측했습니다.
- **live registry 테스트의 고아 모듈 상태 제거(PR #31)** — fixture가 `sys.modules` 별칭 교체 전에 admin app이 보관한 infra 모듈까지 포함해 live registry 상태를 격리하므로, 고아 module alias의 이전 행이 다음 테스트에 유출되지 않습니다.

**측정 근거:** PR #29 최종 전체-suite 순서 실행은 **1551 passed, 32 skipped**였고 영향받던 Outline 3건은 모두 통과했습니다. 표적 검증 **4 passed**, admin/infra subsystem **42 passed**, 고아 alias 축약 순서 재현 **8 passed**였습니다. PR #30 도메인 변경 검증은 Outline **3 passed**, infrastructure UI **4 passed**, TypeScript와 표적 ESLint exit 0이었습니다. PR #31 live registry suite는 **5 passed**, 표적 Ruff도 통과했습니다.

**잔여:** PR #29 전체-suite에는 personal-wiki 파일시스템, migration timeout, systemd installer 쓰기와 관련된 기존 환경 의존 실패 **8건**이 남았습니다. 이번 릴리스 준비에서는 live 외부 통합과 배포를 수행하지 않았습니다.

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
docs/architecture-v1.7.5.md  # 최신 정본 — v1.7.5 v1.7.4 이후 완료 기록(§16.16) 포함
```

## 11. Docs

- [`docs/architecture-v1.7.5.md`](docs/architecture-v1.7.5.md) — 최신 정본 구현 아키텍처이며 v1.7.4 이후 병합 완료 기록(§16.16), Admin Console IA 전환 완료(§16.15), v1.7.3 IA 별칭(§16.14)을 포함한다. 이전 [`docs/architecture-v1.7.4.md`](docs/architecture-v1.7.4.md), [`docs/architecture-v1.7.3.md`](docs/architecture-v1.7.3.md), [`docs/architecture-v1.7.2.md`](docs/architecture-v1.7.2.md)는 과거 기준으로 보존한다.
- [`docs/architecture-v1.7.2-design.md`](docs/architecture-v1.7.2-design.md) — Critical/High hardening 설계(C1/H1–H8, Personal Wiki JWT, 전사 Knowledge Index spec, readiness strict, 분산 상태).
- [`docs/personal-wiki-design.md`](docs/personal-wiki-design.md) — Personal Wiki Vault / extractor / consolidation / memory_service 연동.
- [`docs/security-model.md`](docs/security-model.md) — Dual runtime, untrusted worker, tool policy, data access, egress allowlist.
- [`docs/ha.md`](docs/ha.md) + [`docs/deployment.md`](docs/deployment.md) — HA probes, PDB, HPA, zero-downtime 절차.
- `docs/api/` — Internal Agent Interface, Capability, Approval API.
- `examples/morning-briefing/README.md` — MVP 브리핑 형식(`09:30 / 11:00 / 오늘 반드시 처리`).

## 12. License

**Business Source License 1.1** — see [`LICENSE`](./LICENSE).

- **Licensor:** OpenIT Co., Ltd. / **Licensed Work:** Open Agent OS
- **Additional Use Grant:** Developer Edition 평가·개발·테스트 목적의 production 외 사용 허용, 그 외 production/호스팅/재배포는 Business/Managed 별도 상업 라이선스 필요
- **Change Date:** `2030-08-27`(`v0.1.3` 기준, 이후 버전은 각 릴리즈일로부터 4년) — **Change License:** Apache 2.0 자동 전환 — 제품 버전 `0.1.3`는 아키텍처 `v1.7.2`와 별개
- BSL 원문: https://mariadb.com/bsl11/ · Apache 2.0: https://www.apache.org/licenses/LICENSE-2.0
