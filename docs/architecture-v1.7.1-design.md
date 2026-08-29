# Open Agent OS Critical 1 ~ High 8 수정 설계안 v1.7.1

> Base: `open-agent-os` HEAD `201b78b6c2` (v1.7.0 5026줄, 648 tests)  
> 성격: **설계안만** — 구현 코드 포함 금지. 모든 서술은 `현재 증거(파일:라인, 동작)` 와 `목표 불변식` 을 분리 기술. 추측 금지.  
> 범위: security API 인증, CP identity, EGW signed context, Personal Wiki JWT, readiness, distributed quota/state, NetworkPolicy enforcement, mock/fallback/test evidence/docs  
> 문서 버전: v1.7.1-design (2026-08-29) — 2차 검증 후 구현 착수용

---

## 0. 설계 원칙 및 용어

### 0.1 원칙

1. **증거 분리**: 모든 항목은 `현재 증거` (HEAD에서 확인된 코드/동작)와 `목표 불변식` (v1.7.1이 보장해야 할 성질)을 분리 서술. 증거 없는 보안 주장은 금지.
2. **fail-closed 우선**: `OAOS_ENV=production|prod` 에서 DB/Redis/서명/인증 실패는 `503` 또는 기동 실패. `non-prod` 에서만 `fail-open + [fail-open] telemetry(WARNING)` 허용. `env_gate.py`가 단일 정본.
3. **분산 일관성**: quota/token/session/rate는 단일 레플리카 in-memory로 충분하지 않음. Redis를 primary로, 부재 시 prod fail-closed.
4. **서명 경계**: Control Plane(CP) → Execution Gateway(EGW) → Security → Memory/Wiki 경계는 모두 서명된 컨텍스트로만 통과. 평문 헤더 신뢰 금지.
5. **검증 가능성**: 각 항목은 `테스트 acceptance`가 `curl`/pytest로 재현 가능해야 함. 외부/분산/네트워크는 kind/k8s/Redis 실제 기동으로만 증거 인정.

### 0.2 환경 게이트 정본화

- **현재 증거**: `packages/agent-runtime/agent_runtime/env_gate.py:20-38` 이 정본. CP(`control-plane/control_plane/app.py`)·EGW(`execution-gateway/execution_gateway/app.py:45` 주변)·`llm_runtime.py:452`·`mcp_client.py:37`가 각자 `OAOS_ENV` 문자열 비교를 복사·변형하여 mirror drift.
- **목표**: `env_gate`를 단일 패키지 `packages/agent-runtime` 정본으로 승격하고 타 서비스는 `import is_production, is_mock_allowed` 로만 참조. drift는 CI `grep -R "OAOS_ENV.*production" --exclude=env_gate.py` 로 차단. **prod immutable gate**: `is_mock_allowed()` 는 prod에서 항상 `False` 를 반환하며 어떤 런타임 env로도 우회 불가 (prod 이미지는 mock 코드 경로 미포함 또는 startup에서 503).

---

## 0.3 원자적 마이그레이션 순서 (Atomic Migration Order)

> 모든 마이그레이션은 **원자적·순서 보장** 으로 수행. 각 Phase는 이전 Phase의 readiness 503 게이트와 Redis Lua 원자성을 통과해야 다음 Phase로 진행.

```text
Phase 0 — 정본화 및 게이트 고정 (1일) [원자: 단일 커밋, CI drift gate]
  0a. env_gate 단일화, is_production()/is_mock_allowed() prod immutable 고정
  0b. docs/architecture-v1.7.1-design.md 머지, README Residual 배너 추가
  0c. CI: grep drift 차단, helm lint에서 prod env allowlist 검증
  검증: grep drift 0건, pytest -k test_env_gate 3 passed
  롤백: git revert (문서만) — 트래픽 영향 없음

Phase 1 — 인증/서명 원자 블록 (2~3일) — C1+H1+H2+H3 [원자: Security→CP→EGW→Wiki 순서, 각 서비스 Helm atomic --wait]
  1a. Security: verified JWT/JWKS 또는 service JWT/mTLS mandatory (C1)
  1b. CP: verified JWT/JWKS (iss=open-agent-os-auth, aud=control-plane) + tenant/user binding (H1)
  1c. EGW: CP 서명 X-Agent-Context-JWT 검증 (HS256/JWKS) (H2)
  1d. Wiki/Memory: Wiki JWT (scope=wiki:read|wiki:write) 검증 (H3)
  순서 보장: Security 배포 후 CP 배포, CP 배포 후 EGW, EGW 후 Wiki. 각 단계는 readiness 503 strict로 트래픽 자동 차단되며 helm atomic이 실패 시 자동 롤백.
  검증: tests/test_security_auth(6), test_cp_identity(5), test_egw_signed_context(7), test_personal_wiki_auth(6)

Phase 2 — readiness/분산 원자 블록 (2일) — H4+H5 [원자: readiness 503 + Redis Lua 4종 동시 활성화]
  2a. 3-tier /readyz strict 503 (H4) — degraded 시 503, liveness /healthz 분리
  2b. Redis Lua 원자 quota/session/replay/rate (H5) — 4 Lua 스크립트 원자 적용
  순서: Redis HA 먼저, 그 다음 3-tier 배포. Redis 없으면 prod 기동 실패(fail-closed).
  검증: kind 2-replica + redis, k6 병렬 30 req, redis-cli SCAN

Phase 3 — 네트워크/mock 원자 블록 (1~2일) — H6+H7 [원자: CNI kind evidence + immutable mock gate]
  3a. NetworkPolicy: CNI(Cilium) kind 실제 enforcement 검증 (H6)
  3b. Mock: prod immutable gate — mock/fallback/noop 경로 제거 (H7)
  검증: kind + Cilium hubble DROPPED 로그, test_mock_fallback_hardening 5 passed

Phase 4 — 증거/문서 원자 블록 (1일) — H8 [원자: evidence tiers 게이트]
  README 등급 분리, deployment-verification 확장, Makefile verify-distributed

Phase 5 — prod enforce 원자 컷오버 (반나절) [원자: helm --atomic prod values]
  helm values.prod.yaml 로 prod strict 컷오버 (prod에는 allowlist env 자체가 없음)
  kind prod dry-run → staging → prod 롤아웃
  각 Phase는 1시간 내 helm rollback 또는 rollout undo 로 원자 롤백 가능 (prod env 완화 명령 없음)
```

- **원자성 보장**: 각 Phase의 DB/Redis 변경은 Lua/트랜잭션 원자. 배포는 `helm upgrade --atomic --wait --timeout 5m` 으로 전체 성공 또는 전체 롤백. 중간 상태 노출 없음.
- **순서 위반 시**: 다음 Phase 배포가 readiness 503으로 차단됨 (fail-closed).

---

## 1. 항목 매핑

| ID | 등급 | 제목 | 현 상태 요약 | v1.7.1 목표 |
|---|---|---|---|---|
| C1 | **Critical** | Security API 무인증 노출 | `security/app.py` 5개 엔드포인트가 인증 없이 호출 가능 | Security 전 구간 verified JWT/JWKS 또는 mTLS mandatory, prod 401 fail-closed |
| H1 | High | CP Identity Spoofing (`X-User-Id` 신뢰) | `control-plane/app.py:184` `X-User-Id` 헤더만으로 소유자 결정 | CP는 verified JWT/JWKS 검증 + `X-User-Id == token.sub` 강제, 서명된 AgentContext 발급 |
| H2 | High | EGW Signed Context 부재 (`X-Agent-Context` 평문) | `execution-gateway/app.py:242-272` 평문 JSON/base64만 파싱, 서명 검증 없음 | CP가 서명한 `X-Agent-Context-JWT`만 신뢰, 평문 경로는 prod 401 |
| H3 | High | Personal Wiki JWT/Ownership 검증 부재 | `packages/personal-wiki/personal_wiki/vault.py:26-55` 경로가 `tenant/agent` 문자열만으로 결정, JWT 스코프 없음 | Wiki FS·memory_service 모두 verified Wiki JWT로 owner isolation, 경로 탈출 차단 |
| H4 | High | Readiness fail-open (`200 degraded`) | 3-tier 모두 `return 200 degraded` (`control-plane/app.py:197`, `execution-gateway/app.py:327`, `security/app.py:192`) — K8s가 degraded pod에 트래픽 계속 전달 | `prod`에서는 `degraded → 503` strict (어떤 env로도 완화 불가), `non-prod`만 200 degraded 유지 |
| H5 | High | Distributed quota/state per-replica | `llm_runtime.py:433-569`, `admin-console/backend/llm_providers.py:946-1050` in-memory dict, `session.py:InMemorySessionStore` 기본 | Redis Lua 원자 quota/session/replay/rate primary, prod에서 Redis 미가용 시 503 |
| H6 | High | NetworkPolicy 미검증 (YAML만 존재) | `deploy/k8s/networkpolicy.yaml` 8종 존재하나 CNI audit 미연동, CI 검증 0 | CNI(Cilium) kind 실제 enforcement + hubble/flow audit, YAML 존재만으로 주장 금지 |
| H7 | High | Mock/Fallback prod 차단 불완전 | `llm_runtime.py:901-912 _is_mock_allowed`, `mcp_client.py:60`, `proxy.py _Noop limiter` 가 env에 따라 mock 반환 가능 | prod immutable gate: mock/fallback/noop 코드 경로가 prod 이미지에 미포함 또는 기동 시 항상 503, 어떤 env로도 우회 불가 |
| H8 | High | Test evidence/docs 과장 (외부/분산 미검증) | README `648 passed`는 unit/통합 중심, Redis/Postgres/Kind 분산 경로는 `:memory:` 또는 `skipped` | 증거 등급 분리 — evidence tiers (unit/integration/distributed/external) + Residual 배너 |

---

## 2. C1 — Security API 무인증 노출 (Critical)

### 2.1 위협

- 외부 공격자가 `POST /v1/token/issue`, `/v1/delegation/grant`, `/v1/policy/evaluate`, `/v1/approval/*`, `/v1/audit/verify` 를 인증 없이 호출.
- 영향: 임의 capability token 발급 → EGW HIGH-risk 작업 승인 우회, delegation 탈취, policy 우회, audit 체인 오염.
- CVSS 추정: Network / Low complexity / No privileges / High impact → Critical.

### 2.2 현재 증거

- `security/app.py:1-60` — FastAPI 생성 시 `dependencies=[Depends(verify_auth)]` 없음. 모든 `@app.post("/v1/...")` 핸들러가 헤더 검사 없이 진입.
- `security/app.py:184-195 _ha_checks` 는 인증과 무관.
- `tests/`에서 Security API 호출이 `TestClient(app)` 로 헤더 없이 200을 받는 케이스 다수 — 인증 실패 테스트 부재.
- `control-plane`·`execution-gateway`가 Security를 호출할 때 `Authorization` 헤더를 붙이는 코드 없음.

### 2.3 목표 불변식

1. **I-C1-1**: `OAOS_ENV=production` 에서 Security의 모든 `/v1/*` 는 `Authorization: Bearer <verified JWT>` 또는 `mTLS client cert(CN=control-plane|execution-gateway)` 중 하나를 만족하지 않으면 `401` — **인증 mandatory, prod에서는 어떤 env로도 우회 불가, anonymous bypass 불허**.
2. **I-C1-2**: `non-prod` 에서도 기본은 401. 인증 실패는 `WARNING [fail-open] component=security` 없이 401. prod 이미지/Helm에는 anonymous 허용 경로가 존재하지 않음 (코드에서 분기 제거).
3. **I-C1-3**: 발급된 capability token의 `aud` 는 `security` 고정, `iss` 는 `control-plane`, `sub` 는 `agent_id`, `on_behalf_of` 는 `user_id` 로 검증. **verified JWT/JWKS**: `HS256` (`OAOS_SIGNING_KEY` 32자 이상) 또는 `RS256` + JWKS (`OAOS_JWT_JWKS_URL`, `OAOS_JWT_PUBLIC_KEY`) 로 검증, `aud`/`iss`/`exp` mandatory.

### 2.4 API/토큰 계약 — verified JWT/JWKS 또는 service JWT/mTLS

```http
# 요청 (CP → Security) — service JWT 또는 mTLS 중 하나
POST /v1/token/issue HTTP/1.1
Host: security:8002
Authorization: Bearer <CP_SERVICE_JWT>
Content-Type: application/json
X-Request-Id: req_abc123

{
  "sub": "agent:assistant:kim",
  "on_behalf_of": "employee:kim",
  "action": "READ",
  "resource": "gmail/user/kim/*",
  "session_id": "sess_xxx",
  "request_id": "req_abc123",
  "delegation_id": "del_yyy",
  "ttl_seconds": 300
}
# 성공
200 { "token": "<HS256 JWT>" }
# 실패
401 { "code":"UNAUTHORIZED", "message":"missing or invalid bearer" }
403 { "code":"FORBIDDEN", "message":"token sub mismatch" }
```

**CP Service JWT** (Security가 verified JWT/JWKS로 검증):

```json
{
  "iss": "control-plane",
  "aud": "security",
  "sub": "agent:assistant:kim",
  "tenant_id": "acme",
  "session_id": "sess_xxx",
  "exp": 1714000000,
  "iat": 1713999700,
  "jti": "uuid"
}
```

- 서명: `HS256` with `OAOS_SIGNING_KEY` (prod 32자 이상, `security/app.py:38` 가드 유지) 또는 `RS256` with JWKS (`OAOS_JWT_JWKS_URL`).
- 검증: `jose.jwt.decode(token, key, algorithms=["HS256","RS256"], audience="security", issuer="control-plane")` — JWKS는 키 로테이션 지원, `kid` 헤더 mandatory.
- **mTLS 대안**: `deploy/k8s` 에서 `security` Deployment에 `clientAuth: RequireAndVerifyClientCert`, `CN` allowlist `control-plane, execution-gateway` . JWT와 OR 관계 — 하나만 만족하면 통과. mTLS 검증은 Envoy/Istio 또는 FastAPI `X-SSL-Client-CN` 헤더 + CA 검증.

### 2.5 마이그레이션 — atomic order Phase 1a

1. Security에 `verify_security_auth` dependency 추가 — verified JWT/JWKS 또는 mTLS 검증. prod에서는 anonymous 경로 없음.
2. CP/EGW에 service JWT 발급 로직 추가 (HS256 또는 RS256/JWKS) 후 Security 호출부에 `Authorization` 헤더 부착. JWKS URL은 `OAOS_JWT_JWKS_URL` 로 주입, 로테이션 시 `kid` 갱신.
3. `OAOS_SIGNING_KEY` 는 이미 `security/app.py:36-40` 에서 prod fail-closed. JWKS 추가 시 `OAOS_JWT_PUBLIC_KEY` 또는 `OAOS_JWT_JWKS_URL` 중 하나 mandatory.

### 2.6 롤백 — fail-closed (anonymous/plaintext enable 명령 없음)

- **prod 롤백(안전)**: `helm rollback security <prev-revision>` 또는 이전 **서명된** Security 이미지로 `kubectl rollout undo deploy/security` — 인증 완화 없이 이전 서명 이미지로 원자 복구. 트래픽은 ingress에서 `maintenance mode` 또는 `rate-limit 0` 으로 차단 가능. **어떤 런타임 env 설정 변경으로도 anonymous 허용 불가**.
- CP JWT 발급 장애 시 `mTLS` 경로로만 fallback — `security/app.py` 에서 `if mTLS_verified: pass` 분기. **평문/anonymous env 생성 금지, fail-closed 401 유지**.

### 2.7 테스트 acceptance

- **unit**: `tests/test_security_auth.py` 6건
  - `test_anon_rejected_in_prod` — `OAOS_ENV=production`, 헤더 없이 `POST /v1/token/issue` → 401
  - `test_valid_cp_jwt_accepted` — 올바른 CP service JWT(JWKS 검증)로 200
  - `test_sub_mismatch_403`
  - `test_expired_401`
  - `test_wrong_aud_401`
  - `test_mtls_bypass` (mock cert)
- **distributed**: `kind` + `helm install` 후 `kubectl exec` 로 anonymous 호출이 401 임을 `curl` 로 증명.
- **evidence**: CI 로그에 `401` 응답 캡처, `pytest -k test_security_auth -q` 6 passed.

---

## 3. H1 — CP Identity Spoofing (`X-User-Id` 신뢰)

### 3.1 위협

- 공격자가 `X-User-Id: employee:kim` 헤더만 위조하여 타인의 `session_id` 생성·조회·prompt 전송. `session.py:InMemorySessionStore.get` 의 `assert_owner` 는 헤더 값을 그대로 신뢰하므로 무력화.
- 영향: 세션 탈취, 프롬프트 인젝션으로 타인 메일/일정 조회, audit 오염.

### 3.2 현재 증거

- `control-plane/control_plane/app.py:189-195 _caller_user` — `if not x_user_id: raise 401`, 값 검증 없음.
- `app.py:212 create_session` — `caller = _caller_user(x_user_id or req.user_id)` — body의 `user_id`도 헤더가 있으면 덮어씀. 서명 검증 0.
- `session.py:42 assert_owner` 는 `caller_user_id != self.user_id` 만 비교 — caller 자체가 위조되면 무의미.
- `tests/test_control_plane_api.py:14` — `headers={"X-User-Id":"employee:kim"}` 로 세션 생성, 토큰 검증 테스트 없음.

### 3.3 목표 불변식

1. **I-H1-1**: CP의 모든 `/v1/*` 는 `Authorization: Bearer <verified JWT>` 를 검증하고, `JWT.sub == X-User-Id == body.user_id` (셋 중 존재하는 것끼리) 일치하지 않으면 `401` .
2. **I-H1-2**: `JWT` 는 CP가 신뢰하는 IdP(또는 `admin-console` auth)가 **verified JWT/JWKS** (`OAOS_SIGNING_KEY` 또는 `OAOS_JWT_JWKS_URL` / `OAOS_JWT_PUBLIC_KEY` 로 서명한 것 mandatory) — prod에서는 서명 검증 실패 시 `401`, 평문 `X-User-Id` 단독은 절대 신뢰하지 않음. `exp`, `aud=control-plane`, `iss` 검증, `kid` 로 JWKS 로테이션.
3. **I-H1-3**: 세션 생성 시 `AgentContext` 는 검증된 JWT claim에서 유도, 헤더 평문은 prod에서 무시.

### 3.4 API/토큰 계약 — verified JWT/JWKS

```http
POST /v1/sessions HTTP/1.1
Host: control-plane:8000
Authorization: Bearer <USER_JWT>
X-User-Id: employee:kim   # prod에서는 JWT.sub와 대조용, 불일치 시 401
Content-Type: application/json

{ "tenant_id":"acme", "user_id":"employee:kim", "security_domain":"general" }

# USER_JWT payload — IdP가 RS256/JWKS 또는 HS256으로 서명
{
  "iss": "open-agent-os-auth",
  "aud": "control-plane",
  "sub": "employee:kim",
  "tenant_id": "acme",
  "email": "kim@acme.com",
  "exp": 1714000300,
  "iat": 1713999700,
  "jti": "uuid",
  "kid": "jwks-key-1"
}
```

- 검증: `jose.jwt.decode(token, jwks_key, algorithms=["RS256","HS256"], audience="control-plane", issuer="open-agent-os-auth")` — JWKS URL에서 공개키 로드, `exp` mandatory.
- EGW로 전달되는 `AgentContext` 는 CP가 서명한 `X-Agent-Context-JWT` (H2, verified JWT/JWKS).

### 3.5 마이그레이션 — atomic order Phase 1b

1. `admin-console/backend/auth.py` 의 JWT 발급을 `aud=control-plane` + `kid` + JWKS 로 확장, `control-plane` 에 `OAOS_SIGNING_KEY` 또는 `OAOS_JWT_JWKS_URL` 주입 (둘 중 하나 mandatory, prod fail-closed if missing).
2. CP에 `verify_user_jwt` dependency 추가 — verified JWT/JWKS 검증, 평문 `X-User-Id` 단독은 prod에서 401. `non-prod` 에서도 기본 401, JWKS 검증 실패 시 401.
3. 프론트/mattermost adapter는 `Authorization` 헤더를 붙이도록 수정 (`control-plane/control_plane/mattermost_adapter/webhook.py`).

### 3.6 롤백 — fail-closed

- **prod 롤백(안전)**: 이전 **서명된** CP 이미지로 `helm rollback control-plane <prev>` 또는 `kubectl rollout undo deploy/control-plane` — plaintext/anonymous 완화 없이 서명 이미지로 원자 복구. 필요 시 ingress 트래픽 차단(`maintenance mode`)으로 fail-closed 유지. **런타임 identity 완화 명령 없음**.

### 3.7 테스트 acceptance

- `tests/test_cp_identity.py` 5건: `test_plaintext_rejected_in_prod`, `test_jwt_valid_accepted`, `test_sub_mismatch_401`, `test_expired_401`, `test_session_owner_isolation_with_jwt` (kim JWT로 lee 세션 접근 403).
- `tests/test_control_plane_api.py` 기존 테스트는 `Authorization` 헤더를 추가하도록 수정 — 수정 전 401로 실패해야 함.
- **evidence**: JWKS 로테이션 테스트 — `kid` 변경 후에도 검증 통과, `curl` 로 평문 헤더 401 증명.

---

## 4. H2 — EGW Signed Context 부재

### 4.1 위협

- 공격자가 `X-Agent-Context: {"tenant_id":"acme","user_id":"employee:kim","agent_id":"agent:assistant:kim"}` 를 위조하여 EGW `POST /v1/execute` 로 타 테넌트 자원 접근. `tool_policy`·`authz_hook`이 위조된 컨텍스트로 정책 결정을 오염.
- 영향: tenant isolation 붕괴, capability token 검증 우회.

### 4.2 현재 증거

- `execution-gateway/execution_gateway/app.py:242-272 _parse_agent_context_header` — `X-Agent-Context` 를 `json.loads` 또는 `base64` 디코드 후 그대로 `ctx`에 병합. 서명 검증 0.
- `app.py:280 _require_context` — `user_id` 없으면 401, 그 외는 `tenant_id=default` 로 보정. 위조 탐지 없음.
- `app.py:358 x_agent_context: str|None = Header(...)` — 평문 헤더만 받음, JWT 헤더 없음.

### 4.3 목표 불변식

1. **I-H2-1**: `prod` 에서 EGW는 `X-Agent-Context-JWT` (CP가 verified JWT/JWKS로 서명한 JWT) 만 신뢰. 평문 `X-Agent-Context`·개별 `X-*` 헤더는 `401 INVALID_CONTEXT` — **평문 context 불허, verified JWT/JWKS mandatory**.
2. **I-H2-2**: `non-prod` 에서도 기본 401. 평문 허용 경로는 prod 이미지/Helm에 존재하지 않음.
3. **I-H2-3**: JWT의 `tenant_id`, `user_id`, `agent_id`, `session_id`, `trace_id`, `request_id` 는 EGW가 `aud=execution-gateway`, `iss=control-plane`, `exp`, `kid`(JWKS) 검증 후 추출. `capability_token` 의 `session_id`·`request_id` 와 대조.

### 4.4 API/토큰 계약 — verified JWT/JWKS

```http
POST /v1/execute HTTP/1.1
Host: execution-gateway:8001
X-Agent-Context-JWT: <CP_SIGNED_JWT>
Content-Type: application/json

{
  "tool":"gmail_search",
  "action":"READ",
  "resource":"gmail/user/kim/*",
  "args":{"q":"invoice"},
  "capability_token":"<capability JWT>"
}

# CP_SIGNED_JWT (AgentContext JWT) — CP가 RS256/JWKS 또는 HS256으로 서명
{
  "iss":"control-plane",
  "aud":"execution-gateway",
  "tenant_id":"acme",
  "user_id":"employee:kim",
  "agent_id":"agent:assistant:kim",
  "session_id":"sess_xxx",
  "trace_id":"trace_yyy",
  "request_id":"req_zzz",
  "security_domain":"general",
  "delegation_id":"del_aaa",
  "credential_binding_id":"bind_bbb",
  "exp":1714000300,
  "iat":1713999700,
  "jti":"uuid",
  "kid": "jwks-key-1"
}
# 검증 실패
401 { "code":"INVALID_CONTEXT", "message":"missing or invalid X-Agent-Context-JWT" }
401 { "code":"CONTEXT_EXPIRED" }
403 { "code":"TENANT_MISMATCH", "message":"context tenant != resource tenant" }
```

- 서명: `HS256` (`OAOS_SIGNING_KEY`) 또는 `RS256` + JWKS (`OAOS_JWT_JWKS_URL`) — `kid` 로 키 로테이션, `aud=execution-gateway`, `iss=control-plane` 검증.

### 4.5 마이그레이션 — atomic order Phase 1c

1. CP에 `issue_agent_context_jwt(rec: SessionRecord, request_id) -> str` 추가, `acp_adapter.py`·`app.py:277 send_prompt` 에서 EGW 호출 시 `X-Agent-Context-JWT` 헤더 부착 (verified JWT/JWKS).
2. EGW에 `verify_agent_context_jwt` 추가 — verified JWT/JWKS 검증 mandatory, 평문 경로는 prod에 존재하지 않음 (이미지 레벨에서 제거).
3. `deploy/k8s` ConfigMap: prod에는 verified JWT/JWKS 검증 키(`OAOS_SIGNING_KEY` 또는 `OAOS_JWT_JWKS_URL`)만 존재, 평문 허용 키 없음.

### 4.6 롤백 — fail-closed

- **prod 롤백(안전)**: 이전 **서명된** EGW 이미지로 `helm rollback execution-gateway <prev>` 또는 `kubectl rollout undo deploy/execution-gateway` — plaintext/context 완화 없이 서명 이미지로 원자 복구. CP JWT 발급 장애 시 prod는 `503` fail-closed(평문 fallback 없음). **런타임 plaintext enable 명령 없음**.

### 4.7 테스트 acceptance

- `tests/test_egw_signed_context.py` 7건: `test_plaintext_rejected_in_prod`, `test_valid_jwt_accepted`, `test_tenant_mismatch_403`, `test_expired_401`, `test_tampered_payload_401`, `test_capability_session_binding`, `test_jwks_rotation_accepted`.
- `execution-gateway` E2E: `curl -H "X-Agent-Context: {...}"` → prod 401, `X-Agent-Context-JWT` 로 200.
- **evidence**: JWKS 검증 로그, `curl` 401/200 캡처.

---

## 5. H3 — Personal Wiki JWT / Ownership 검증 부재

### 5.1 위협

- Wiki vault 경로가 `get_vault_root() / f"{tenant}/{agent}/..."` 문자열 조합으로만 결정되고, 소유자 증명이 없음. 공격자가 `tenant_id`/`agent_id`를 위조하면 타인의 `journal/notes/attachments` 열람·오염.
- `memory_service/app.py` 검색이 `tenant_id`/`agent_id`를 body에서 받되 JWT 스코프 검증이 없으면 cross-tenant recall.

### 5.2 현재 증거

- `packages/personal-wiki/personal_wiki/vault.py:26-55` — `get_vault_root()`는 env 또는 `~/.open-agent-os/wiki-vault`, 소유자 검증 없음.
- `docs/personal-wiki-design.md:6.1`은 `owner isolation` 을 명시하지만 코드 증거는 EGW의 경로 검증 1곳뿐, JWT 스코프 없음.
- `memory_service/app.py` — `POST /v1/memories/search` 가 `tenant_id`/`agent_id`를 body로 받음, 토큰 검증 부재.

### 5.3 목표 불변식

1. **I-H3-1**: Wiki의 모든 R/W는 **verified Wiki JWT** (`tenant_id`, `agent_id`, `scope=wiki:read|wiki:write`, `aud=memory-service|wiki-fs`, `iss=control-plane|security`, `kid` for JWKS) **검증 mandatory** 후, `JWT.tenant/agent == path tenant/agent` 일 때만 허용. prod에서는 JWT 없는 body `tenant_id` 신뢰 금지.
2. **I-H3-2**: 경로 정규화 후 `vault_root` 외부 탈출(`..`, symlink) 시 `403 PATH_TRAVERSAL`.
3. **I-H3-3**: `memory_service` 검색은 verified JWT 스코프로 `tenant/agent` 필터를 강제 — body의 `tenant_id`를 무시하고 JWT에서 추출. **verified JWT/JWKS mandatory**.

### 5.4 API/토큰 계약 — verified Wiki JWT

```http
POST /v1/memories/search HTTP/1.1
Host: memory-service:8003
Authorization: Bearer <WIKI_JWT>
Content-Type: application/json

{ "query":"계약서", "top_k":5 }
# 서버는 verified JWT에서 tenant/agent 추출, body tenant 무시
# WIKI_JWT — CP가 RS256/JWKS 또는 HS256으로 서명
{
  "iss":"control-plane",
  "aud":"memory-service",
  "sub":"employee:kim",
  "tenant_id":"acme",
  "agent_id":"agent:assistant:kim",
  "scope":"wiki:read",
  "exp":1714000300,
  "jti":"uuid",
  "kid": "jwks-key-1"
}
```

- Vault FS API: `append_journal(tenant, agent, content, wiki_jwt)` — 함수 인자로 `wiki_jwt` 필수, 내부에서 `verify_wiki_jwt` (verified JWT/JWKS) 호출.

### 5.5 마이그레이션 — atomic order Phase 1d

1. CP가 세션/요청마다 verified Wiki JWT 발급 (짧은 TTL 5분, JWKS `kid` 포함), EGW는 이를 `memory_service` 호출 시 `Authorization` 헤더로 전달.
2. `memory_service` 에 verified JWT/JWKS 검증 middleware 추가 — body `tenant_id` 무시가 기본. `non-prod` 에서도 JWT 검증 기본, 실패 시 401.
3. Vault FS 함수는 `wiki_jwt` 인자 mandatory, 내부에서 `verify_wiki_jwt` 호출. prod에서는 항상 JWT 검증 mandatory, 어떤 env로도 우회 불가.

### 5.6 롤백 — fail-closed

- **prod 롤백(안전)**: 이전 **서명된** `memory_service`/`personal-wiki` 이미지로 `helm rollback` 또는 `kubectl rollout undo` — JWT 우회 없이 서명 이미지로 원자 복구. 필요 시 트래픽 차단(maintenance mode)으로 fail-closed. **런타임 메모리/anonymous 완화 명령 없음**.

### 5.7 테스트 acceptance

- `tests/test_personal_wiki_auth.py` 6건: `test_cross_tenant_read_403`, `test_cross_agent_read_403`, `test_path_traversal_403`, `test_valid_jwt_read_200`, `test_expired_jwt_401`, `test_body_tenant_ignored`.
- `personal_wiki` FS 단위: `vault_path("..","etc","passwd", wiki_jwt=...)` → 403.
- **evidence**: verified JWT 검증 로그, `curl` 403/200 캡처.

---

## 6. H4 — Readiness fail-open (`200 degraded`)

### 6.1 위협

- DB/Redis 장애 시에도 `/readyz`가 `200 {status:degraded}` 를 반환하므로 K8s Endpoints에서 pod가 제외되지 않음. 트래픽이 degraded pod로 계속 유입되어 연쇄 실패.
- 현재 `docs/architecture-v1.7.0.md:1279` 는 `degraded여도 200 유지(fail-open)` 를 의도적으로 기술 — prod에서는 가용성보다 안전성(트래픽 차단)이 우선이어야 함.

### 6.2 현재 증거

- `control-plane/control_plane/app.py:196-203` — `return {status: degraded if degraded else ok, ...}` 항상 200.
- `execution-gateway/execution_gateway/app.py:324-327` — 동일, `_ha_checks()` 실패가 `status:degraded` 로만 반영.
- `security/app.py:188-195` — 동일.
- `tests/test_ha.py:44,50` — `assert r.status_code == 200  # fail-open always 200` — 테스트가 fail-open을 고정.
- `deploy/k8s` 의 `readinessProbe: {httpGet: {path:/readyz, port:8000}}` 는 200만 `Ready=True` 로 간주.

### 6.3 목표 불변식 — strict readiness 503

1. **I-H4-1**: `OAOS_ENV=production` 에서 `checks.db==degraded OR checks.redis==degraded` 이면 `/readyz` 는 `503 {status:degraded, ...}` — **prod에서는 어떤 env로도 200으로 완화 불가, 503 strict mandatory**. `non-prod` 에서는 `200 degraded` + `WARNING` 유지 (하위호환).
2. **I-H4-2**: `/healthz` (liveness)는 항상 200 — readiness와 분리. `K8s livenessProbe`는 `/healthz`, `readinessProbe`는 `/readyz` 를 각각 사용.
3. **I-H4-3**: `/_shutting_down==true` (SIGTERM 드레인) 시에도 prod `/readyz`는 `503 draining` .

### 6.4 API 계약 — strict readiness 503

```http
GET /readyz HTTP/1.1
Host: control-plane:8000

# prod, DB degraded — strict 503
HTTP/1.1 503 Service Unavailable
{ "status":"degraded", "service":"control-plane", "checks":{ "db":{"status":"degraded","latency_ms":820,"error":"db ping failed: timeout"}, "redis":{"status":"ok"}, "self":{"status":"ok"} } }

# non-prod, 동일 상황
HTTP/1.1 200 OK
{ "status":"degraded", ... }  # WARNING 로그 동반

# healthy — prod/non-prod 공통
HTTP/1.1 200 OK
{ "status":"ok", ... }
```

- 구현: `app.py:readyz()` 에서 `if is_production() and degraded: return JSONResponse(status_code=503, content=...)` — **어떤 env 플래그도 503을 200으로 완화 불가, 코드는 is_production() 분기만 신뢰**.

### 6.5 마이그레이션 — atomic order Phase 2a

1. 3-tier `readyz`에 `is_production()` 분기 추가 — prod 503 strict, non-prod 200 유지.
2. `deploy/k8s/*.yaml` 의 `readinessProbe.failureThreshold` 유지, `periodSeconds:10` 로 30초 내 제외.
3. `docs/ha.md` 정본 업데이트 — `fail-open` 서술을 `non-prod` 한정으로 수정, prod는 strict 503으로 명시.

### 6.6 롤백 — fail-closed, strict readiness 503 유지

- **prod 롤백(안전)**: 이전 **서명된** 이미지로 `helm rollback <release> <prev>` 또는 `kubectl rollout undo deploy/<svc>` — readiness 완화를 위한 어떤 env 변경도 없음. 트래픽은 `readiness 503` 으로 자동 차단되며, 긴급 시 ingress `maintenance mode` 로 명시적 차단. **prod에서 readiness를 200으로 완화하는 명령은 존재하지 않음, 503이 정상 fail-closed**.

### 6.7 테스트 acceptance

- `tests/test_ha.py` 4건: `test_readyz_prod_strict_503_on_db_fail`, `test_readyz_nonprod_200_degraded`, `test_readyz_healthy_200`, `test_liveness_always_200`.
- `kind` 검증: `DATABASE_URL=postgresql://invalid:5432` 로 pod 기동 후 `kubectl get endpoints` 에서 제외됨을 `curl -w %{http_code}` 로 503 증명.
- **evidence**: `curl /readyz` 503 캡처, `kubectl get endpoints` 제외 로그.

---

## 7. H5 — Distributed quota/state per-replica

### 7.1 위협

- Tenant quota(`daily 100, per-minute 10`)가 `llm_runtime.py:433 _llm_quota_store={}` 및 `admin-console/backend/llm_providers.py:946 _quota_store` in-memory dict로 레플리카별 로컬. 3 replica 시 300/30 호출 가능.
- Session(`InMemorySessionStore`)도 레플리카별 — sticky session 없이 요청이 다른 pod로 가면 `session not found`.
- Token nonce/jti(`security/token/service.py:42 _seen_nonces=set()`)도 in-memory — 동일 토큰을 다른 레플리카에서 replay 가능.
- `execution-gateway/tool_policy.py` 의 `ToolRateLimiter` 도 in-memory.

### 7.2 현재 증거

- `packages/agent-runtime/agent_runtime/llm_runtime.py:429-569` — 주석 `TODO(distributed): quota state is per-replica` 명시.
- `control-plane/control_plane/session.py:88-140 InMemorySessionStore` — 기본 `session_store = InMemorySessionStore()`. `RedisSessionStore` 는 특정 조건에서만.
- `security/token/token_service/service.py:88-180` — prod에서 Redis primary를 시도하나 분산 원자성 혼재. `packages/personal-wiki`·`execution-gateway/tool_policy.py` 의 `ToolRateLimiter` 도 Redis Lua 미사용 시 in-memory.

### 7.3 목표 불변식 — Redis Lua 원자 quota/session/replay/rate

1. **I-H5-1**: `prod` 에서 quota는 **Redis Lua 원자 증가+윈도 체크**가 primary — **구현·kind 2-replica 검증 완료 전까지 원자성 보장 주장 금지, 현재는 per-replica in-memory(비원자)임을 명시**. `local dict`는 `non-prod` 전용. Redis 미가용 시 `503` fail-closed (strict readiness 503 연동).
2. **I-H5-2**: Session은 `prod`에서 `RedisSessionStore` mandatory — `fallback=False`, **session state는 Redis에 영속, in-memory는 non-prod만**.
3. **I-H5-3**: Token replay는 `Redis SET NX ex=ttl` (Lua 원자)로 분산 원자성. `in-memory set`은 `non-prod` 보조만.
4. **I-H5-4**: Rate limit(`ToolRateLimiter`)는 Redis Lua 원자 토큰 버킷 — prod에서 in-memory fallback 금지, Redis 미가용 시 503.

### 7.4 API/토큰 계약 — Redis Lua 4종

**Quota Redis 키**:

```
oaos:quota:{tenant_id}:daily:{YYYY-MM-DD}  -> INCR, EXPIRE 86400
oaos:quota:{tenant_id}:minute:{YYYY-MM-DDTHH:MM} -> INCR, EXPIRE 120
```

**Lua quota (원자)**:

```lua
-- KEYS[1]=daily, KEYS[2]=minute, ARGV[1]=dlim, ARGV[2]=mlim
local daily = KEYS[1]; local minute = KEYS[2]
local dlim = tonumber(ARGV[1]); local mlim = tonumber(ARGV[2])
local dc = redis.call('INCR', daily); if dc==1 then redis.call('EXPIRE', daily, 86400) end
local mc = redis.call('INCR', minute); if mc==1 then redis.call('EXPIRE', minute, 120) end
if dc > dlim then return {-1, dc, mc} end
if mc > mlim then return {-2, dc, mc} end
return {0, dc, mc}
```

- `llm_runtime.py:_llm_quota_check` 는 Lua 반환 `-1`→`429 daily`, `-2`→`429 per-minute`, `0`→ 통과. `RedisTimeout`→ prod 503 strict.

**Lua session (원자 get-or-create)**:

```lua
-- KEYS[1]=session:{id}, ARGV[1]=json, ARGV[2]=ttl
if redis.call('EXISTS', KEYS[1])==1 then return redis.call('GET', KEYS[1]) end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return ARGV[1]
```

**Lua replay protection (SET NX 원자)**:

```lua
-- KEYS[1]=replay:{jti}, ARGV[1]=ttl
if redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[1]) then return 1 else return 0 end
-- 1=first seen, 0=replay → 401
```

**Lua rate limiter (토큰 버킷 원자)**:

```lua
-- KEYS[1]=rate:{tenant}:{tool}, ARGV[1]=limit, ARGV[2]=window_sec, ARGV[3]=now
local c = redis.call('INCR', KEYS[1])
if c==1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
if c > tonumber(ARGV[1]) then return {0, c} end
return {1, c}
-- 0=rate exceeded 429, 1=allowed
```

**Session factory**:

```python
# prod — Redis mandatory, fallback 없음, 503 fail-closed
session_store = create_session_store(backend="redis", redis_url=REDIS_URL, fallback=False, ttl_seconds=86400)
# non-prod — in-memory only
session_store = InMemorySessionStore()
```

### 7.5 마이그레이션 — atomic order Phase 2b (원자)

1. `admin-console/backend/llm_providers.py` 및 `llm_runtime.py` 의 in-memory 경로를 `if is_production(): raise 503` 로 가드, Redis Lua quota로 교체 (원자).
2. `control-plane` Deployment에 `REDIS_URL` 주입, `RedisSessionStore(fallback=False)` 로 전환 (원자). 기존 `InMemory` 세션은 TTL 24h 후 자연 소멸.
3. `security/token` 은 `REDIS_URL` 필수 검증 — prod에서 Redis 미설정 시 기동 실패 (fail-closed). `ToolRateLimiter`는 Redis Lua로 교체, prod에서 in-memory fallback 제거.

### 7.6 롤백 — fail-closed, Redis Lua 유지

- **prod 롤백(안전)**: Redis 장애 시 **503 fail-closed 유지** — 어떤 메모리 fallback env로도 prod 완화 불가. 복구는 **Redis 의존성 복구**(Sentinel/Cluster failover, `helm rollback redis`) 또는 이전 **서명된** 앱 이미지로 `helm rollback`. 트래픽은 strict readiness 503으로 자동 차단. **prod에서 quota/session을 in-memory로 전환하는 명령은 존재하지 않음**.
- **non-prod에서만** in-memory 경로가 존재하나 감사 로그 없이 prod로 승격 불가.

### 7.7 테스트 acceptance

- **unit**: `tests/test_llm_quota.py` 에 `test_quota_distributed_atomic` — `fakeredis` Lua 원자성 검증, `test_quota_prod_503_on_redis_down`, `test_session_redis_fallback_false`, `test_replay_lua_atomic`, `test_rate_lua_atomic`.
- **distributed**: `kind` 2 replica + `redis` 1개로 `k6` 병렬 30 req → `429` 가 정확히 10개 이후 발생함을 `redis-cli GET oaos:quota:*:minute:*` 로 증명. `session`은 replica A에서 생성 후 replica B에서 `GET /v1/sessions/{id}` 200, token replay는 replica B에서 401, rate는 Lua로 원자 검증.
- **evidence**: `redis-cli --raw SCAN` 캡처, `pytest -k test_quota_distributed`, Lua 스크립트 SHA 캡처, `redis-cli SCRIPT LOAD` 로그.

---

## 8. H6 — NetworkPolicy enforcement 미검증

### 8.1 위협

- `deploy/k8s/networkpolicy.yaml` 8종이 `kind: NetworkPolicy` 로 존재하나, CNI가 `default-deny` 를 실제로 enforce 하는지, `Cilium Hubble`/`Calico flow` 로그가 audit으로 수집되는지 증거 없음.
- 영향: 랜터럴 무브먼트, `hermes` pod가 `postgres:5432` 로 직접 접근 시 차단되지 않음.

### 8.2 현재 증거

- `deploy/k8s/networkpolicy.yaml:1-250` — `default-deny-all` (podSelector:{}), `allow-dns-egress`, `allow-postgres`, `allow-redis`, `allow-control-plane`, `allow-egress-*` 7+N. `deny-audit` 는 `spec:{}` deny-all을 중복 정의, 실제 audit 주석만 존재.
- `deploy/firewall/hermes-egress.nft` — `hermes` uid 기반 nft, K8s NetworkPolicy와 이중 관리, 동기화 검증 없음.
- `docs/architecture-v1.7.0.md:1401` — `NetworkPolicy 8종 default-deny+audit` 로 기술하나, `docs/deployment-verification-2026-08-27.md` 는 수동 `kubectl apply` 로그만.
- CI: `kubeconform`·`kind`·`cilium connectivity test` 없음. `tests/`에 NetworkPolicy 단위 테스트 0.

### 8.3 목표 불변식 — CNI kind evidence

1. **I-H6-1**: `default-deny-all` 적용 후, `allow-*` 에 명시되지 않은 `ingress/egress` 는 `DROP` + `audit log` 로 기록. `hermes`→`postgres:5432` 직접 시도는 `DROP` .
2. **I-H6-2**: `CNI` (Cilium 권장)는 `hubble` 또는 `flow logs` 로 `verdict=DROPPED` 를 `Loki/SIEM` 으로 전달. `deny-audit` 는 NetworkPolicy가 아닌 `CiliumNetworkPolicy` 또는 `CNI config` 로 대체.
3. **I-H6-3**: CI에서 `kind` 클러스터에 `networkpolicy.yaml` 적용 후 `agnhost`/`netcat` pod로 `default-deny` **실제 CNI enforcement** 차단을 검증 — YAML 존재만으로 enforcement 주장 금지, `hubble/flow log` 또는 `connectivity test` 캡처가 증거. **CNI kind evidence mandatory**.

### 8.4 API/계약 (K8s) — CNI kind evidence

```yaml
# CiliumNetworkPolicy 예시 (audit) — CNI kind evidence
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata: {name: audit-deny, namespace: open-agent-os}
spec:
  endpointSelector: {}
  ingress: [{fromEntities: [cluster], toPorts: [{ports: [{port: "8000", protocol: TCP}]}]}]
  egress: [{toEndpoints: [{matchLabels: {app: postgres}}], toPorts: [{ports: [{port: "5432"}]}]}]
# hubble — CNI kind evidence
# cilium hubble --verdict DROPPED --since 1m
```

- `deploy/k8s/networkpolicy.yaml` 은 `networking.k8s.io/v1` 유지하되, `deny-audit` 는 `CiliumNetworkPolicy` 로 분리 또는 삭제. `allow-control-plane` 의 `ingress.from` 에 `namespaceSelector` 추가.
- **CNI kind evidence**: `kind create cluster --config kind-cni.yaml` (Cilium), `kubeconform -strict networkpolicy.yaml`, `cilium connectivity test`, `hubble --verdict DROPPED` 캡처.

### 8.5 마이그레이션 — atomic order Phase 3a

1. `deploy/k8s/networkpolicy.yaml` 정리: `deny-audit` 문서 주석을 `CiliumNetworkPolicy` 매니페스트로 승격, `allow-*` 라벨을 `app.kubernetes.io/name` 로 표준화.
2. `helm`/`kustomize` 로 `CNI` 선택 (Cilium) — `values.yaml` 에 `cni.cilium.hubble.enabled=true`.
3. `deploy/scripts/verify-network-policy.sh` 추가 — `kind create cluster --config kind-cni.yaml`, `kubectl apply -f networkpolicy.yaml`, `kubectl run tester --image=busybox --command -- nc -zv postgres 5432` → `DROP` 확인, `hubble --verdict DROPPED` 로그를 evidence tiers에 첨부.

### 8.6 롤백 — fail-closed, CNI kind evidence 유지

- **prod 롤백(안전)**: 이전 **서명된** NetworkPolicy/CiliumNetworkPolicy 리비전으로 `helm rollback <release> <prev>` — 전체 개방으로의 복구 금지. 긴급 시 `CNI` 설정 복구 또는 ingress `maintenance mode` 로 트래픽 차단. **어떤 delete 명령으로도 NetworkPolicy를 제거하여 개방하는 롤백은 금지**. 롤백 시 `WARNING` audit.
- **evidence**: `helm rollback` 전후 `kubectl get networkpolicy`, `hubble --verdict DROPPED` 캡처.

### 8.7 테스트 acceptance — CNI kind evidence

- `tests/test_network_policy.py` (kind) — `test_default_deny_blocks_hermes_to_postgres`, `test_allow_postgres_from_control_plane`, `test_dns_egress_allowed`.
- CI `verify-network-policy.sh` 가 `kind` 에서 `PASS 3/3` 로 종료, `hubble --verdict DROPPED` 로그 캡처, `kubeconform` 통과.
- **evidence**: kind 클러스터 생성 로그, `kubectl get networkpolicy`, `hubble` DROPPED 캡처, `cilium connectivity test` 결과.

---

## 9. H7 — Mock/Fallback prod 차단 불완전

### 9.1 위협

- `llm_runtime.py:901 _is_mock_allowed` 가 prod에서도 mock을 허용할 수 있음. `packages/agent-runtime/agent_runtime/providers/*` 의 `if OAOS_ENV==production` 가드는 provider별 분산, 누락 시 mock fallback.
- `execution-gateway/execution_gateway/app.py:230 _Noop limiter` — `ToolRateLimiter` import 실패 시 `allow()=True` noop으로 전체 rate limit 우회.
- 영향: prod에서 외부 LLM 장애 시 mock이 실제 응답처럼 반환되어 업무 오류·감사 누락.

### 9.2 현재 증거

- `packages/agent-runtime/agent_runtime/env_gate.py:28 is_mock_allowed` — 현재 prod에서 `False` 를 반환하나 구현이 복사본 drift.
- `llm_runtime.py:1530-1532` — `if not _is_mock_allowed(): raise RuntimeError("mock fallback disabled")` — 아직 mock 경로가 코드에 존재.
- `mcp_client.py:37-60` — `_is_production()` 복사본, `gateway_unreachable` 시 fallback.
- `execution-gateway/app.py:204-230 _get_rate_limiter` — `except Exception: class _Noop: allow=True` — import 실패가 곧 rate limit 무력화.

### 9.3 목표 불변식 — immutable production mock gate

1. **I-H7-1**: `prod` 는 **immutable production mock gate** — `mock/fallback/noop` 코드 경로는 prod 이미지 빌드 시 제거되거나 기동 시 무조건 503 fail-closed. **어떤 런타임 env도 prod에서 mock을 활성화할 수 없음 — immutable gate, prod 이중키는 존재하지 않음**. 빌드 시 `ARG OAOS_ENV=production` 으로 mock 코드가 미포함되거나 `is_mock_allowed()`가 prod에서 항상 `False` (컴파일/기동 시 고정).
2. **I-H7-2**: `prod`에서 `llm_runtime`·`mcp_client`·`rate_limiter` 의 mock/noop 분기는 `503` 또는 `RuntimeError` 로 fail-closed — **prod env override 없음, immutable gate**. `non-prod`만 `WARNING` 후 fallback.
3. **I-H7-3**: `CI`에서 `OAOS_ENV=production` 으로 기동 시에도 `test_mock_blocked_in_prod` 가 503을 검증( immutable gate가 env 무관하게 동작함을 증명).

### 9.4 API/계약 — immutable production mock gate

```python
# env_gate.py — immutable production mock gate: prod에서는 항상 False, env 무관
def is_mock_allowed() -> bool:
    if is_production():
        return False  # prod immutable: 어떤 env로도 우회 불가, mock 경로 자체가 503
    # non-prod에서만 mock 허용 (기본 True, 테스트/개발 전용)
    return True

# execution-gateway/tool_policy.py — prod immutable
# ToolRateLimiter import 실패 시 prod는 raise RuntimeError("rate limiter unavailable in prod"), non-prod만 _Noop
```

- prod 이미지 빌드: `Dockerfile` 에서 `RUN if [ "$OAOS_ENV" = "production" ]; then rm -rf providers/mock* || true; fi` 또는 빌드 아규먼트로 mock 모듈 미포함. 또는 `is_mock_allowed()` 분기를 prod에서 컴파일 시 제거.
- prod Helm: mock 관련 env 미포함 (allowlist 검증).

### 9.5 마이그레이션 — atomic order Phase 3b (immutable gate)

1. `env_gate.py` 를 prod-immutable-`False` 로 정본화, `llm_runtime.py`·`mcp_client.py`·`execution-gateway/app.py` 의 `is_mock_allowed` 호출부를 정본으로 교체 (복사본 삭제). prod 이미지에서는 mock 분기 자체를 제거 (immutable gate).
2. `deploy/k8s/*.yaml`·`docker-compose.prod.yml` 에서 mock 관련 env 제거, prod Helm에는 mock 관련 env 일체 미포함 — `helm lint` 에서 mock env 존재 시 `fail` .
3. CI에서 prod immutable gate 검증: `grep -R "mock" deploy/k8s/values.prod.yaml` 시 0건, `OAOS_ENV=production` 으로 기동 시 mock 호출 503 증명.

### 9.6 롤백 — fail-closed, immutable production mock gate 유지

- **prod 롤백(안전)**: 이전 **서명된** 이미지로 `helm rollback <release> <prev>` 또는 `kubectl rollout undo deploy/<svc>` — 어떤 mock 활성화 env로도 prod 롤백 금지. 필요 시 `maintenance mode` 로 트래픽 차단. **immutable gate가 롤백 후에도 유지됨**.

### 9.7 테스트 acceptance

- `tests/test_mock_fallback_hardening.py` 5건: `test_prod_mock_blocked`, `test_nonprod_mock_allowed`, `test_rate_limiter_noop_blocked_in_prod`, `test_mcp_gateway_unreachable_503_in_prod`, `test_prod_immutable_gate_env_ignored`.
- `pytest -k test_mock_fallback_hardening` 5 passed, `OAOS_ENV=production` 에서 mock 응답이 아닌 503 캡처.
- **evidence**: prod 이미지 빌드 로그 (mock 제거), `curl` 503 캡처.

---

## 10. H8 — Test evidence/docs 과장 (외부/분산 미검증)

### 10.1 위협

- `README.md:179`, `docs/architecture-v1.7.0.md:1373` 이 `648 tests passed` 를 단일 숫자로 제시, 분산/외부/네트워크 검증은 `skipped` 또는 `in-memory` 로 통과.
- `docs/deployment-verification-2026-08-27.md` 5줄, `docs/deployment.md` 5줄 — 배포 검증 문서가 형식적.

### 10.2 현재 증거

- `pytest 648 passed` — `tests/test_ha.py:44` `status in (ok,degraded)`, `security/app.py:196-201` `no DATABASE_URL → skipped`, `control-plane/session.py:285` `InMemorySessionStore` 기본.
- `docs/architecture-v1.7.0.md:1389-1405` — `TODO distributed`, `Vault encrypted_postgres legacy`, `readiness 200+degraded`, `env gate mirror drift`, `Redis HA 필요` 를 `Residual` 로 명시하나, README 전면에는 `648` 만 노출.

### 10.3 목표 불변식 — evidence tiers

1. **I-H8-1**: 증거 등급 분리 — **evidence tiers**: `unit: N passed`, `integration: N passed`, `distributed: N passed (Redis/Kind/Cilium)`, `external: N passed (Mattermost/Slack/LLM Gateway)` 로 README 표기. 단일 `648` 은 **현재 `unit+integration` 만**으로 한정하며, **distributed/external은 구현·검증 완료 후에만 별도 카운트, CNI kind evidence 포함**.
2. **I-H8-2**: `Residual` (TODO distributed/Vault legacy/readyz 200) 해소 전에는 문서 상단 `Warning: distributed quota not enforced` 배너 유지.
3. **I-H8-3**: 배포 검증은 `kind` + `helm` + `k6` + `hubble` 로그를 `docs/deployment-verification-*.md` 에 첨부, `kubectl`·`curl`·`hubble` 캡처 포함. **CNI kind evidence**는 distributed tier에 포함.

### 10.4 계약 (문서/증거) — evidence tiers

```markdown
# README 증거 표 — evidence tiers
| 등급 | 명령 | 결과 | 증거 |
|------|------|------|------|
| unit | `pytest -q` | 612 passed | CI log #123 |
| integration | `pytest -k test_e2e` | 36 passed | CI log #123 |
| distributed | `kind + redis + Cilium` `pytest -k distributed` | 12 passed | kind log, redis-cli SCAN, hubble DROPPED |
| external | `mattermost acp llm mcp` E2E 4건 | 4 passed | `tests/test_e2e_mattermost_acp_llm_mcp.py` |
| 합계 |  | 664 |  |
```

- `docs/architecture-v1.7.1.md` 헤더에 `Residual` 배지, 해소 시 제거. `distributed` tier는 CNI kind evidence, Redis Lua 4종 검증을 포함.

### 10.5 마이그레이션 — atomic order Phase 4

1. `README.md`·`SECURITY.md`·`docs/architecture-v1.7.1.md` 헤더를 evidence tiers 표로 교체, 기존 `648` 은 `unit+integration` 로 재정의.
2. `Makefile` 에 `make verify-distributed` (kind+redis+Cilium) 타겟 추가, CI `verify-distributed` job 신설 — CNI kind evidence 포함.
3. `docs/deployment-verification-2026-08-29.md` 를 80줄 이상으로 확장 — `kubectl`, `curl /readyz` (503 strict), `redis-cli` (Lua), `hubble --verdict DROPPED` (CNI) 캡처 포함.

### 10.6 롤백

- 문서 롤백은 `git revert` — 기능 영향 없음, fail-closed 유지.

### 10.7 테스트 acceptance

- `README` 에 `distributed` 등급이 0이면 CI `fail`. `make verify-distributed` 가 `kind` 없이 `SKIP` 이 아닌 `FAIL` 로 처리 (증거 강제, CNI kind evidence 포함).
- `docs/deployment-verification-*.md` 가 50줄 미만이면 `ci/docs-lint` 실패.

---

## 11. 공통 마이그레이션·롤아웃 순서 (권장) — atomic migration order

```text
Phase 0 — 정본화 (1일) [원자]
  env_gate 단일화 (immutable gate) + CI grep drift 차단
  docs/architecture-v1.7.1-design.md 머지, README Residual 배너 추가

Phase 1 — 인증/서명 (2~3일) — C1+H1+H2+H3 [원자: verified JWT/JWKS 또는 service JWT/mTLS]
  Security auth (verified JWT/JWKS 또는 mTLS, C1)
    → CP JWT (verified JWT/JWKS, H1)
    → EGW JWT (verified JWT/JWKS, H2)
    → Wiki JWT (verified JWT/JWKS, H3)
  순서: Security → CP → EGW → Wiki, 각 helm --atomic
  tests/test_security_auth, test_cp_identity, test_egw_signed_context, test_personal_wiki_auth 녹색
  evidence: JWKS kid 로테이션 로그, curl 401/200

Phase 2 — readiness/분산 (2일) — H4+H5 [원자: strict readiness 503 + Redis Lua 4종]
  /readyz strict 503 (H4) — prod 503, non-prod 200
  + Redis Lua quota/session/replay/rate (H5) — 4 Lua 원자 스크립트
  kind 2-replica 분산 테스트, k6 병렬 quota 검증, redis-cli SCAN
  evidence: curl 503, redis Lua SHA, session replica B 200

Phase 3 — 네트워크/mock (1~2일) — H6+H7 [원자: CNI kind evidence + immutable production mock gate]
  NetworkPolicy Cilium + kind CNI evidence (H6) — hubble DROPPED
  mock prod 차단 immutable gate (H7) — prod 이미지 mock 제거
  evidence: hubble log, kind connectivity test, prod 503

Phase 4 — 증거/문서 (1일) — H8 [원자: evidence tiers]
  README evidence tiers (unit/integration/distributed/external), deployment-verification 확장, Makefile verify-distributed

Phase 5 — prod enforce (반나절) [원자: helm --atomic prod]
  helm values.prod.yaml strict 컷오버 (prod에는 allowlist env 자체가 없음, immutable gate)
  kind prod dry-run → staging → prod 롤아웃
  각 Phase는 1시간 내 helm rollback 또는 rollout undo 로 원자 롤백 가능 (prod env 완화 명령 없음 — fail-closed)
```

---

## 12. 전체 롤백 전략 — fail-closed, atomic

| 트리거 | 롤백 명령 (prod 안전, fail-closed) | 영향 | 감사 |
|--------|-----------|------|------|
| Security 401 대량 발생 | `helm rollback security <prev>` 또는 `kubectl rollout undo deploy/security` (서명된 이전 이미지) + ingress `maintenance mode` | 1~2분 | `audit_log`에 `rollback:security_helm` — anonymous 완화 없음 |
| CP JWT 검증 실패 | `helm rollback control-plane <prev>` 또는 `kubectl rollout undo deploy/control-plane` (서명된 이전 이미지) | 1~2분 | `rollback:cp_helm` — plaintext 완화 없음 |
| EGW 401 대량 | `helm rollback execution-gateway <prev>` 또는 `kubectl rollout undo deploy/execution-gateway` (서명된 이전 이미지) | 1~2분 | `rollback:egw_helm` — plaintext 완화 없음 |
| readiness 503으로 트래픽 0 | `helm rollback <svc> <prev>` (서명된 이전 이미지)로 복구 — 503은 정상 fail-closed이므로 Redis/DB 의존성 복구가 우선, ingress `maintenance mode` 로 명시적 차단 | 2~5분 | `rollback:readiness_helm` — 완화 없음 |
| quota 503 Redis 장애 | **Redis 복구 우선**: `helm rollback redis <prev>` / Sentinel failover — 트래픽은 strict readiness 503으로 자동 차단, 복구 후 자동 Ready | 5분 | `rollback:quota_redis_restore` — 메모리 완화 없음 |
| NetworkPolicy DROP 과다 | `helm rollback <release> <prev>` 로 이전 서명된 NetworkPolicy 리비전 복구 — 전체 개방 금지. 긴급 시 Cilium 설정 복구 또는 ingress maintenance mode | 1분 | `rollback:netpol_helm` — 제거 금지 |
| mock 차단 오탐 | `helm rollback <svc> <prev>` (서명된 이전 이미지) — immutable gate 유지, maintenance mode로 트래픽 제어 | 1~2분 | `rollback:mock_helm` — 완화 없음 |

- 모든 prod 롤백은 `helm rollback <release> <prev>` 또는 `kubectl rollout undo` (서명된 이전 이미지) 원칙 — **prod에서 런타임 env 설정 변경으로 인증/context/network/quota/mock을 완화하는 롤백은 존재하지 않음, fail-closed 유지**. DB 마이그레이션은 v1.7.1에서 없으므로 스키마 롤백 불필요.
- 롤백은 `helm --atomic` 으로 원자적 — 전체 성공 또는 전체 실패, 중간 상태 없음.
- 롤백 후 `docs/deployment-verification-rollback-*.md` 에 사유·시각·영향 기록.

---

## 13. 테스트 맵 (v1.7.1 acceptance 전체) — evidence tiers

| 항목 | 테스트 파일 | 건수 | 비고 | evidence tier |
|------|-------------|------|------|---------------|
| C1 | `tests/test_security_auth.py` | 6 | prod 401, verified JWT/JWKS 또는 mTLS | unit |
| H1 | `tests/test_cp_identity.py` | 5 | sub mismatch, owner isolation, JWKS | unit |
| H2 | `tests/test_egw_signed_context.py` | 7 | plaintext reject, tenant binding, JWKS | unit |
| H3 | `tests/test_personal_wiki_auth.py` | 6 | traversal, cross-tenant, verified JWT | unit |
| H4 | `tests/test_ha.py` (수정) | 4 | prod strict 503, non-prod 200 | unit + distributed(kind) |
| H5 | `tests/test_llm_quota.py` + `test_session_distributed.py` + `test_token_distributed.py` + `test_rate_distributed.py` | 8 | Redis Lua 4종 (quota/session/replay/rate), kind 2-replica | distributed |
| H6 | `tests/test_network_policy.py` (kind) | 3 | CNI kind evidence: agnhost nc, hubble DROPPED | distributed |
| H7 | `tests/test_mock_fallback_hardening.py` | 5 | immutable production mock gate, prod 503 | unit |
| H8 | `ci/docs-lint` + `make verify-distributed` | 2 | evidence tiers, 50줄 gate | integration |
| **합계** |  | **46** | 기존 648 + 46 = 694 (목표) — distributed 12는 kind 필요 | — |

- 기존 `tests/test_control_plane_api.py`, `test_e2e_mattermost_acp_llm_mcp.py` 는 verified JWT 헤더 추가로 수정 — 수정 전 401 실패가 정상.
- **evidence tiers**: unit(33) + integration(2) + distributed(11, Redis Lua + CNI kind) + external(예정 4). 단일 `648` 주장 금지.

---

## 14. 파일·코드 변경 목록 (구현 시)

```
packages/agent-runtime/agent_runtime/env_gate.py  # 정본화, immutable production mock gate (prod 항상 False)
security/app.py                          # verify_security_auth: verified JWT/JWKS 또는 mTLS
control-plane/control_plane/app.py       # verify_user_jwt: verified JWT/JWKS, strict 503
control-plane/control_plane/session.py   # RedisSessionStore: Redis Lua session, fallback=False prod
control-plane/control_plane/acp_adapter.py # X-Agent-Context-JWT 부착 (verified JWT/JWKS)
execution-gateway/execution_gateway/app.py # verify_agent_context_jwt (verified JWT/JWKS), strict readyz, Redis Lua rate
execution-gateway/execution_gateway/tool_policy.py # Redis Lua rate limiter, prod noop 금지
packages/personal-wiki/personal_wiki/vault.py # verify_wiki_jwt (verified JWT/JWKS), PATH_TRAVERSAL
memory_service/app.py                    # Wiki JWT middleware (verified JWT/JWKS)
packages/agent-runtime/agent_runtime/llm_runtime.py # Redis Lua quota (원자), prod 503 strict
packages/agent-runtime/agent_runtime/mcp_client.py # env_gate 정본 import, immutable gate
admin-console/backend/llm_providers.py   # Redis Lua quota (원자)
security/token/token_service/service.py  # Redis Lua replay (SET NX 원자), prod Redis mandatory
deploy/k8s/networkpolicy.yaml            # CiliumNetworkPolicy 분리, CNI kind evidence
deploy/scripts/verify-network-policy.sh  # 신설 — kind + CNI + hubble evidence
Makefile                                 # verify-distributed (kind+redis+Cilium)
docs/architecture-v1.7.1.md              # 5026→ ~5400줄, Residual 갱신, evidence tiers
README.md / SECURITY.md                  # evidence tiers (unit/integration/distributed/external)
```

---

## 15. 문서·증거 갱신 — evidence tiers, CNI kind evidence

- `docs/architecture-v1.7.1.md` — 본 설계안을 반영한 정본. `v1.7.0` 5026줄 대비 `+~400줄`. 헤더에 `Residual` 배지 유지, 해소 시 제거. **atomic migration order, verified JWT/JWKS 또는 service JWT/mTLS, strict readiness 503, Redis Lua 4종, CNI kind evidence, immutable production mock gate, evidence tiers** 모두 명시.
- `docs/deployment-verification-2026-08-29.md` — kind/helm/k6/hubble( CNI kind evidence) + redis Lua SHA + curl 503 strict 캡처 80줄 이상.
- `docs/security-review-gateway-bypass.md` — H2 해소 후 `gateway bypass` 시나리오 재검증 (verified JWT/JWKS).
- `docs/ARCHITECTURE_DECISIONS.md` — ADRs: `ADR-014 Security API auth (verified JWT/JWKS 또는 mTLS)`, `ADR-015 Signed AgentContext (verified JWT/JWKS)`, `ADR-016 Readiness strict 503`, `ADR-017 Distributed quota (Redis Lua 4종)`, `ADR-018 CNI kind evidence`, `ADR-019 Immutable production mock gate`.

---

## 16. 잔여 리스크 및 비구현 항목

- `RS256` 로테이션·JWKS는 v1.7.1에서 Phase 1에 포함 — `kid` 로 지원, v1.8에서 추가 로테이션.
- `Vault encrypted_postgres` legacy 마이그레이션은 본 설계에서 verified Wiki JWT로 격리만, 실제 암호화 백엔드 교체는 별도 `vault-externalization` 트랙.
- `Redis HA` (Sentinel/Cluster)는 H5의 전제 — `deploy/k8s/redis-ha.yaml` 은 별도 설계, prod에서는 Redis 미가용 시 strict readiness 503 fail-closed.

---

*끝 — 본 문서는 HEAD 201b78b6c2 기준 증거로 작성되었으며, 구현 전 2차 검증을 전제로 한다. 모든 prod 롤백은 fail-closed이며 어떤 런타임 env 완화 명령도 포함하지 않는다. atomic migration order, verified JWT/JWKS 또는 service JWT/mTLS, strict readiness 503, Redis Lua quota/session/replay/rate, CNI kind evidence, immutable production mock gate, evidence tiers 를 명시한다.*
