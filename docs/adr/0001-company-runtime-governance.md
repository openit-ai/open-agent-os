# ADR 0001 — Company 런타임 거버넌스 연결 (O2·O3)

- 상태: **제안(Proposed)** — Hermes 설계 검토 후 승인 시 `docs/architecture.md` §4.3·§6과 `docs/company-design.md`에 반영
- 날짜: 2026-11-17
- 관련: `docs/company-design.md` §10 O2(런타임 미연결)·O3(OAuth·Slack 이벤트 미구현), `docs/architecture.md` §4.3·§6
- 근거 조사: Hermes Agent 소스(커밋 `3b42157858`, 2026-11-09)의 플러그인·게이트웨이 확장 지점

## 1. 문제

Company 거버넌스(정책 판정·JIT 승인·감사·개인 자격 격리·LLM 라우팅)는 v2.2.0에서 **DB 수준**(RLS·트리거·권한·SQL 함수)으로만 강제된다. 비서(Hermes 런타임)의 요청 경로는 `authorize_action`·`identity_isolation_decision`·`llm_route_decision`을 호출하지 않으므로, 비서가 도구를 실행하거나 모델을 고를 때 정책이 적용되지 않는다. 또 G11(Workspace/365 OAuth)·G12(Slack 이벤트)를 받는 수신 경로가 없어 연동 단계의 "실제 왕복 증거"를 쓰는 주체가 없다.

## 2. Hermes가 제공하는 확장 지점 (조사 요약)

| 필요 | Hermes 지점 | 판정 |
|---|---|---|
| 도구 호출 차단·승인 | 플러그인 훅 `pre_tool_call` → `block`/`approve`/`modify` 반환. 내장·플러그인·MCP 도구 모두 통과. async 지원, 시간 초과·예외 시 **차단(fail-closed)**, 제한 `plugins.hook_callback_timeout`(기본 30초, 최대 600초) | 사용 |
| 요청자 식별 | 훅 인자에는 없음 → `gateway.session_context.get_session_env("HERMES_SESSION_ACTOR_ID" 등)` (공개 플러그인 API) | 사용 |
| 관리자 전용 승인 | Hermes 기본 승인 게이트는 **요청자 본인도 승인 가능**(`gateway/approval_actor.py`) | **사용 불가** → OAOS가 자체 처리 |
| 모델 라우팅 | 턴별 라우터 훅 없음. `llm_request` 미들웨어로 호출마다 `model` 교체 가능(provider·base_url은 불가) | LLM 게이트웨이 뒤에서 사용 |
| 감사 이벤트 | `post_tool_call`·`pre/post_llm_call`·`post_api_request`·세션 훅, 무코드 `hooks.outbound` 서명 웹훅 | 사용 |
| 사용자별 자격 격리 | 프로필별 `.env`·시크릿·MCP·플러그인 + `gateway.profile_routes`(user_id → 프로필). `register_session_env`는 하위 프로세스에만 적용 | 프로필 사용 |
| OAuth·Slack 이벤트 수신 | 게이트웨이에 범용 HTTP 라우트 없음. 플러그인 OAuth는 프로필 단위 CLI 로그인 | **별도 서비스** |
| 배포 | `hermes plugins install owner/repo --ref <sha> --enable`, 프로필마다 설치. 문서화된 `PluginContext`·훅은 하위 호환(최소 2개 마이너 릴리스 폐기 예고) | 사용 |

## 3. 결정 (제안)

**Hermes 플러그인 1개 + 기존 Company 서비스 확장 + 구성원별 프로필**을 조합한다. 지원되는 확장 지점만 쓰고 Hermes 내부를 패치하지 않는다.

### 3.1 구성

```
구성원(Mattermost/Slack/Telegram)
   │
Hermes gateway ── profile_routes: user_id → 구성원 프로필(개인 .env·MCP·기본 모델)
   │   └─ oaos-governance 플러그인 (모든 프로필에 설치)
   │        pre_tool_call ──HTTP(유닉스 소켓)──▶ Company 거버넌스 API ──▶ PostgreSQL(company.*)
   │        post_tool_call / post_api_request / 세션 훅 ──▶ 감사 기록
   │        llm_request 미들웨어 ──▶ 라우팅 판정으로 model 교체
   │
LLM 게이트웨이(OpenAI 호환, 기존 G1/G13 키) ◀── 모델 호출
Company 서비스(127.0.0.1:8765 / nginx /company/) ── 콘솔 + 거버넌스 API + /company/oauth/callback + Slack 이벤트
```

### 3.2 Company 거버넌스 API (기존 `oaos-company.service` 확장)

- 위치: 같은 단일 프로세스(`console_app.py` 계열)에 `/company/api/v1/*` 추가. 기본 수신은 **유닉스 소켓**(`~/.config/oaos-company/governance.sock`, 0600), 외부 노출 없음. nginx는 OAuth 콜백·Slack 이벤트 경로만 공개.
- 인증: 플러그인 → API는 서비스 토큰(Company `.env`, 프로필별 발급·폐기 가능). 요청마다 `actor_id`(Hermes 세션 ACTOR), `profile`, `session_key`, `request_id`를 보내고 API가 `company.request_id` GUC로 감사에 연결.
- 엔드포인트(초안):
  - `POST /authorize` — `{action, digest, target, approval_id?}` → `allow | deny | pending(approval_id, expires_at)`; 내부적으로 `authorize_action`·`identity_isolation_decision` 호출
  - `POST /approvals` / `GET /approvals/{id}` — JIT 요청·상태(결정은 콘솔에서 **다른 관리자**만)
  - `POST /route` — `{task_class, sensitivity}` → `{model, fallback}` (`llm_route_decision`)
  - `POST /audit` — 도구·모델 호출 결과 요약(본문·비밀 없음, 기존 민감 키 검사 적용)
- 실패 처리: API 무응답·오류 → 플러그인은 **차단**(Hermes 훅 fail-closed와 일치). 읽기 전용 안전 도구 목록(정책의 `allow` 캐시, TTL ≤ 60초)만 예외.

### 3.3 도구 분류와 액션 매핑

- 플러그인은 도구 이름과 인자로 정책 액션을 만든다: `tool.<toolset>.<name>`(예 `tool.terminal.run`, `tool.file.write`, `mcp.<server>.<tool>`), `digest` = 정규화한 인자 SHA-256(비밀·본문은 해시만).
- 기본 정책 번들(C07)에 도구 액션 기본값 추가: 읽기 계열 `allow`, 쓰기·외부 전송·실행 계열 `approval`, 관리·자격 변경 `deny`. 조직이 콘솔에서 조정.

### 3.4 JIT 승인 흐름 (관리자 전용 보장)

1. `pre_tool_call` → `/authorize`가 `pending` 반환
2. 플러그인은 Hermes 기본 승인 게이트(`approve`)를 **쓰지 않고** `block`을 반환한다. 메시지: "관리자 승인 대기(요청 #id, 10분) — 승인 후 다시 요청하세요"
3. 콘솔(또는 Mattermost/Slack 관리자 알림 링크)에서 **다른 관리자**가 결정 → 기존 C07 `decide_approval`
4. 사용자가 같은 작업을 다시 요청하면 `pre_tool_call`이 `approval_id`와 `digest`로 `/authorize` → `allow`(1회 소비)
- 선택: 짧은 대기(≤ 120초) 동안 폴링 후 허용하는 모드는 훅 시간 제한 안에서만, 기본은 끔.

### 3.5 자격 격리 (owner == credential == provider == output)

- 구성원마다 Hermes **프로필**을 만들고 `gateway.profile_routes`로 해당 user_id를 연결한다. 개인 OAuth 토큰·MCP 서버·기본 모델은 프로필에만 둔다.
- 개인 토큰 원본은 Company Vault(AES-GCM)에 두고, 프로필 `.env`에는 **짧은 수명 접근 토큰만** 플러그인/동기화 작업이 써 넣는다(회수 시 즉시 삭제 — C05·C17 회수와 연동).
- 공유 대화(그룹 채널)에서는 ACTOR 기준으로 판정하고, 개인 자격이 필요한 도구는 개인 DM 프로필에서만 허용.

### 3.6 모델 라우팅

- Hermes는 OpenAI 호환 **LLM 게이트웨이**(LiteLLM 등, Company 서버 loopback)를 provider로 쓴다. `llm_request` 미들웨어가 턴마다 `/route` 판정으로 `model`만 바꾼다. 사용량 과금 모델은 C16·C07 승인(다른 관리자) 없이는 라우팅 후보에 오르지 않는다(기존 016 제약 유지).

### 3.7 OAuth·Slack 이벤트 (O3)

- `/company/oauth/callback`(이미 nginx 경로 존재)에서 구성원 본인 위임을 처리: 콘솔 로그인 세션 → state(HMAC, 10분) → 공급자 동의 → 토큰을 소유자 바인딩 Vault 행으로 저장 → `integration/external_roundtrip` 감사 기록(이것이 C11~C14 "실제 왕복 증거"의 기록 주체).
- Slack 이벤트: `/company/slack/events`에서 서명 검증 후 Hermes 웹훅 플랫폼 또는 Slack 어댑터로 전달, 수신 왕복을 감사 기록.

## 4. 대안과 기각 사유

| 대안 | 기각 사유 |
|---|---|
| Hermes 기본 승인 게이트(`approve`) 사용 | 요청자 본인이 승인 가능 → "다른 관리자 결정" 요구 위반 |
| 외부 프록시만(MCP·도구 프록시) | 터미널·파일 등 내장 도구가 우회 |
| 프로필 없이 단일 `.env` | 구성원 간 자격 격리 불가 |
| Hermes 내부 패치 | 업그레이드마다 깨짐, 공개 API 아님 |

## 5. 위험·한계

- 플러그인은 같은 프로세스에서 돈다 — 샌드박스가 아니므로 플러그인 코드 자체를 최소화·고정 버전(`--ref <sha>`)으로 설치하고 verify에서 해시 확인.
- 훅 시간 제한(최대 600초) — 승인 대기는 차단 후 재요청 방식이 기본.
- ACTOR는 공개 API의 ContextVar에 의존 — Hermes 버전 고정과 업그레이드 시 회귀 스모크 필요.
- 프로필 증가에 따른 운영 부담(플러그인·설정 중복) — Company 설치기가 프로필 생성·플러그인 설치·라우팅을 자동화(새 Phase).

## 6. 구현 계획 (승인 시)

| 단계 | 내용 | 완료 판정 |
|---|---|---|
| R1 | 거버넌스 API(유닉스 소켓·서비스 토큰·`/authorize`·`/audit`) + 도구 액션 기본 정책 | DB 스모크 + API 단위 테스트(거부·대기·허용·만료·재사용) |
| R2 | `oaos-governance` Hermes 플러그인(`pre_tool_call`·`post_tool_call`·세션 훅) + 설치·해시 고정 | 모의 Hermes 훅 호출 스모크, fail-closed 확인 |
| R3 | 구성원 프로필 자동화(`profile_routes`) + Vault → 프로필 단기 토큰 동기화·회수 | 두 구성원 교차 접근 거부 read-back |
| R4 | LLM 게이트웨이 + `llm_request` 라우팅 | 기본·프리미엄·폴백·과금 승인 read-back |
| R5 | OAuth 콜백·Slack 이벤트 → 왕복 증거 | C11~C14 verified 경로(실서비스는 V3) |

새 Company Phase 번호(C18~)와 게이트 영향은 R1 착수 전 `docs/company-plan.md`에 반영한다.
