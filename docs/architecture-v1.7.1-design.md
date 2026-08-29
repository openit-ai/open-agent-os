# Open Agent OS Critical 1 ~ High 8 수정 설계안 v1.7.1

> Base: `open-agent-os` HEAD `932e558868` (v1.7.0 5026줄, 648 tests — 현재 상태)  
> 성격: **설계안만** — 구현 코드 포함 금지. 모든 서술은 `현재 증거(파일:라인, 동작)` 와 `목표 불변식` 을 분리 기술. 추측 금지.  
> 범위: security API 인증, CP identity, EGW signed context, Personal Wiki JWT, readiness, distributed quota/state, NetworkPolicy enforcement, mock/fallback/test evidence/docs  
> 문서 버전: v1.7.1-design (2026-08-29) — 2차 검증 후 구현 착수용
> 현재 상태 vs 목표: HEAD 932e558868은 v1.7.0 증거(5026줄, 648 tests, in-memory/distributed 미검증, readiness 200 degraded, NetworkPolicy YAML만, 평문 context, mock env 가능)를 그대로 보유 — v1.7.1 목표는 아래 불변식을 모두 충족한 상태에서만 구현 착수로 인정.

---

## 0. 설계 원칙 및 용어

### 0.1 원칙

1. **증거 분리**: 모든 항목은 `현재 증거` (HEAD에서 확인된 코드/동작)와 `목표 불변식` (v1.7.1이 보장해야 할 성질)을 분리 서술. 증거 없는 보안 주장은 금지. 목표가 미달성 시 문서에 `Residual` 로 명시.
2. **fail-closed 우선**: `OAOS_ENV=production|prod` 에서 DB/Redis/서명/인증 실패는 `503` 또는 기동 실패. `non-prod` 에서만 `fail-open + [fail-open] telemetry(WARNING)` 허용. `env_gate`가 단일 정본이며 prod에서 완화 경로가 존재하지 않음(immutable).
3. **분산 일관성**: quota/token/session/rate는 단일 레플리카 in-memory로 충분하지 않음. Redis를 primary로, 부재 시 prod fail-closed. prod에서 in-memory fallback은 없음.
4. **서명 경계**: Control Plane(CP) → Execution Gateway(EGW) → Security → Memory/Wiki 경계는 모두 서명된 컨텍스트로만 통과. 평문 헤더 신뢰 금지. 평문 경로는 prod 401/403.
5. **검증 가능성**: 각 항목은 `테스트 acceptance`가 `curl`/pytest로 재현 가능해야 함. 외부/분산/네트워크는 kind/k8s/Redis 실제 기동으로만 증거 인정. 문서상 “원자성 보장” 주장은 통합 검증 전 금지.

### 0.2 환경 게이트 정본화

- **현재 증거**: `packages/agent-runtime/agent_runtime/env_gate.py:20-38` 이 정본 후보. CP(`control-plane/control_plane/app.py`)·EGW(`execution-gateway/execution_gateway/app.py:45` 주변)·`llm_runtime.py:452`·`mcp_client.py:37`가 각자 `OAOS_ENV` 문자열 비교를 복사·변형하여 mirror drift.
- **목표**: 단일 패키지 `packages/env-gate` (또는 `packages/agent-runtime/env_gate.py` 정본 유지) 하나만 정본으로 승격하고 타 서비스는 `import`로만 참조. drift는 CI `grep -r "OAOS_ENV.*production" --exclude=env_gate.py` 로 차단. prod에서 완화 env는 존재하지 않으므로 게이트는 단일 함수 `is_production() -> bool` 만 제공.

---

## 1. 항목 매핑

| ID | 등급 | 제목 | 현 상태 요약 (HEAD 932e558868 증거) | v1.7.1 목표 |
|---|---|---|---|---|
| C1 | **Critical** | Security API 무인증 노출 | `security/app.py` 5개 엔드포인트가 인증 없이 호출 가능 | 모든 Security API는 mTLS 또는 `Authorization: Bearer <JWT>` 필수, prod immutable 401 |
| H1 | High | CP Identity Spoofing (`X-User-Id` 신뢰) | `control-plane/app.py:184` `X-User-Id` 헤더만으로 소유자 결정 | CP는 `Authorization` 검증 + `X-User-Id == token.sub` 강제, 서명된 AgentContext 발급 |
| H2 | High | EGW Signed Context 부재 (`X-Agent-Context` 평문) | `execution-gateway/app.py:242-272` 평문 JSON/base64만 파싱, 서명 검증 없음 | CP가 서명한 `X-Agent-Context-JWT`만 신뢰, 평문 경로는 prod 401 |
| H3 | High | Personal Wiki JWT/Ownership 검증 부재 | `packages/personal-wiki/personal_wiki/vault.py:26-55` 경로가 `tenant/agent` 문자열만으로 결정, JWT 스코프 없음 | Wiki FS·memory_service 모두 `wiki JWT (tenant, agent, scope)` 로 owner isolation, 경로 탈출 차단 |
| H4 | High | Readiness fail-open (`200 degraded`) | 3-tier 모두 `return 200 degraded` (`control-plane/app.py:197`, `execution-gateway/app.py:327`, `security/app.py:192`) — K8s가 degraded pod에 트래픽 계속 전달 | `prod`에서는 `degraded → 503` strict, `non-prod`만 200 degraded + `Ready=False` 조건 문서화 |
| H5 | High | Distributed quota/state per-replica | `llm_runtime.py:433-569`, `admin-console/backend/llm_providers.py:946-1050` in-memory dict, `session.py:InMemorySessionStore` 기본 | Redis Lua/SET NX primary, prod에서 Redis 미가용 시 503, in-memory는 prod에서 사용 불가 |
| H6 | High | NetworkPolicy 미검증 (YAML만 존재) | `deploy/k8s/networkpolicy.yaml` 8종 존재하나 CNI audit 미연동, CI 검증 0, `deny-audit`가 `spec:{}` deny-all 중복 | CNI(Cilium/Calico) flow/audit 로그를 SIEM으로, CI에서 `kubeconform`+`kind`로 `default-deny` 실제 차단 검증 |
| H7 | High | Mock/Fallback prod 차단 불완전 | `llm_runtime.py:901-912 _is_mock_allowed`, `mcp_client.py:60`, `proxy.py _Noop limiter` 가 prod에서도 mock 반환 가능 | prod는 immutable startup gate: mock/fallback/noop 코드 경로 자체가 prod 이미지에 미포함 또는 기동 시 항상 503 |
| H8 | High | Test evidence/docs 과장 (외부/분산 미검증) | README `648 passed`는 unit/통합 중심, Redis/Postgres/Kind 분산 경로는 `:memory:` 또는 `skipped` (`security/app.py:196-201`, `test_ha.py:44`) 로 통과 | 증거 등급 분리 (`unit` vs `distributed` vs `external`), `5026줄/648` 주장에 `distributed: N passed (Redis/Kind)` 별도 표기, `TODO distributed` 해소 전에는 문서에 `Residual` 명시 유지 |

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
- `control-plane`·`execution-gateway`가 Security를 호출할 때 `Authorization` 헤더를 붙이는 코드 없음 (`control-plane/control_plane/acp_adapter.py:303` 은 `Bearer {api_key}` 를 Hermes에만 붙임).

### 2.3 목표 불변식

1. **I-C1-1**: `OAOS_ENV=production` 에서 Security의 모든 `/v1/*` 는 `Authorization: Bearer <JWT>` 또는 `mTLS client cert(CN=control-plane|execution-gateway)` 중 하나를 만족하지 않으면 `401` — **인증 mandatory, prod에서 완화 불가, immutable**.
2. **I-C1-2**: `non-prod` 에서도 기본은 401. prod 이미지/Helm에서는 익명 허용 경로가 존재하지 않으며 코드는 익명 분기를 포함하지 않음. 실패 시 `WARNING [fail-open] component=security reason=anon_rejected` 가 아닌 `401` 로 고정.
3. **I-C1-3**: 발급된 capability token의 `aud` 는 `security` 고정, `iss` 는 `control-plane`, `sub` 는 `agent_id`, `on_behalf_of` 는 `user_id` 로 검증.

### 2.4 API/토큰 계약

```http
# 요청 (CP → Security)
POST /v1/token/issue HTTP/1.1
Host: security:8002
Authorization: Bearer <CP_JWT>
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
403 { "code":"FORBIDDEN", "message":"token sub mismatch" }  # CP JWT sub != body.sub
```

**CP JWT** (Security가 검증):

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

- 서명: `HS256` with `OAOS_SIGNING_KEY` (prod 32자 이상, `security/app.py:38` 가드 유지). 차기에는 `RS256` 로테이션 고려.
- 검증: `jose.jwt.decode(token, OAOS_SIGNING_KEY, algorithms=["HS256"], audience="security", issuer="control-plane")`.

**mTLS 대안**: `deploy/k8s` 에서 `security` Deployment에 `clientAuth: RequireAndVerifyClientCert`, `CN` allowlist `control-plane, execution-gateway` . JWT와 OR 관계 — 하나만 만족하면 통과.

### 2.5 마이그레이션

1. Security에 `verify_security_auth` dependency 추가. CP/EGW에 JWT 발급 로직 추가 후 Security 호출부에 `Authorization` 헤더 부착. 동일 릴리스에서 인증 발급과 검증을 원자적으로 배포 — 별도 완화된 플래그 없이 shadow 로그(검증 결과를 메트릭으로만 기록) 1~2시간 관찰 후 즉시 enforcement로 전환.
2. `OAOS_SIGNING_KEY` 는 이미 `security/app.py:36-40` 에서 prod fail-closed. 추가 시크릿 없음.
3. prod Helm/manifest에는 익명 허용 관련 env/분기가 존재하지 않음 — 코드 레벨에서 제거.

### 2.6 롤백

- **prod 롤백(안전)**: 이전 **서명된** Security 이미지로 `helm rollback security <prev-revision>` 또는 `kubectl rollout undo deploy/security` — 트래픽은 ingress에서 `maintenance mode` 또는 `read-only` 로 차단 가능.
- CP JWT 발급 장애 시 `mTLS` 경로로만 fallback — `security/app.py` 에서 `if mTLS_verified: pass` 분기. prod에서 완화 경로는 없음.
- 그 외에는 트래픽 드레인 또는 전체 중지로 fail-closed 유지.

### 2.7 테스트 acceptance

- **unit**: `tests/test_security_auth.py` 6건
  - `test_anon_rejected_in_prod` — `OAOS_ENV=production`, 헤더 없이 `POST /v1/token/issue` → 401
  - `test_valid_cp_jwt_accepted` — 올바른 CP JWT로 200
  - `test_sub_mismatch_403`
  - `test_expired_401`
  - `test_wrong_aud_401`
  - `test_mtls_bypass` (mock cert)
- **distributed**: `kind` + `helm install` 후 `kubectl exec` 로 anon 호출이 401 임을 `curl` 로 증명.
- **evidence**: CI 로그에 `401` 응답 캡처, `pytest -k test_security_auth -q` 6 passed.

---

## 3. H1 — CP Identity Spoofing (`X-User-Id` 신뢰)

### 3.1 위협

- 공격자가 `X-User-Id: employee:kim` 헤더만 위조하여 타인의 `session_id` 생성·조회·prompt 전송. `session.py:InMemorySessionStore.get` 의 `assert_owner` 는 헤더 값을 그대로 신뢰하므로 무력화.
- 영향: 세션 탈취, 프롬프트 인젝션으로 타인 메일/일정 조회, audit 오염.

### 3.2 현재 증거

- `control-plane/control_plane/app.py:189-195 _caller_user` — `if not x_user_id: raise 401`, 값 검증 없음.
- `app.py:212 create_session` — `caller = _caller_user(x_user_id or req.user_id)` — body의 `user_id`도 헤더가 있으면 덮어씀. 서명 검증 0.
- `app.py:277 send_prompt` / `316 get_agent_context` 동일.
- `session.py:42 assert_owner` 는 `caller_user_id != self.user_id` 만 비교 — caller 자체가 위조되면 무의미.
- `tests/test_control_plane_api.py:14` — `headers={"X-User-Id":"employee:kim"}` 로 세션 생성, 토큰 검증 테스트 없음.

### 3.3 목표 불변식

1. **I-H1-1**: CP의 모든 `/v1/*` 는 `Authorization: Bearer <JWT>` 를 검증하고, `JWT.sub == X-User-Id == body.user_id` (셋 중 존재하는 것끼리) 일치하지 않으면 `401` .
2. **I-H1-2**: `JWT` 는 CP가 신뢰하는 IdP(또는 `admin-console` auth)가 `OAOS_SIGNING_KEY` 또는 `OAOS_JWT_PUBLIC_KEY` 로 **서명한 것 mandatory** — prod에서는 서명 검증 실패 시 `401`, 평문 `X-User-Id` 단독은 절대 신뢰하지 않음. `exp`, `aud=control-plane`, `iss` 검증.
3. **I-H1-3**: 세션 생성 시 `AgentContext` 는 검증된 JWT claim에서 유도, 헤더 평문은 prod에서 무시.

### 3.4 API/토큰 계약

```http
POST /v1/sessions HTTP/1.1
Host: control-plane:8000
Authorization: Bearer <USER_JWT>
X-User-Id: employee:kim   # prod에서는 JWT.sub와 대조용, 불일치 시 401
Content-Type: application/json

{ "tenant_id":"acme", "user_id":"employee:kim", "security_domain":"general" }

# USER_JWT payload
{
  "iss": "open-agent-os-auth",
  "aud": "control-plane",
  "sub": "employee:kim",
  "tenant_id": "acme",
  "email": "kim@acme.com",
  "exp": 1714000300,
  "iat": 1713999700,
  "jti": "uuid"
}
```

- 검증 실패: `401 {code:"IDENTITY_MISMATCH", message:"X-User-Id != token.sub"}` 또는 `401 {code:"TOKEN_EXPIRED"}`.
- EGW로 전달되는 `AgentContext` 는 CP가 `OAOS_SIGNING_KEY` 로 서명한 `X-Agent-Context-JWT` (H2).

### 3.5 마이그레이션

1. `admin-console/backend/auth.py` 의 JWT 발급을 `aud=control-plane` 로 확장, `control-plane` 에 `OAOS_SIGNING_KEY` 또는 `JWKS` 주입.
2. CP에 `verify_user_jwt` dependency 추가. 평문 `X-User-Id` 단독 경로는 prod에서 제거 — 코드는 항상 JWT 검증을 수행하며 평문만으로는 인증되지 않음. shadow 검증(평문 요청을 401로 거부하면서 메트릭 기록) 후 즉시 enforcement.
3. 프론트/ mattermost adapter는 `Authorization` 헤더를 붙이도록 수정 (`control-plane/control_plane/mattermost_adapter/webhook.py`).

### 3.6 롤백

- **prod 롤백(안전)**: 이전 **서명된** CP 이미지로 `helm rollback control-plane <prev>` 또는 `kubectl rollout undo deploy/control-plane` — 필요 시 ingress 트래픽 차단(`maintenance mode`) 또는 `read-only` 로 fail-closed 유지. 완화 경로는 없음.

### 3.7 테스트 acceptance

- `tests/test_cp_identity.py` 5건: `test_plaintext_rejected_in_prod`, `test_jwt_valid_accepted`, `test_sub_mismatch_401`, `test_expired_401`, `test_session_owner_isolation_with_jwt` (kim JWT로 lee 세션 접근 403).
- `tests/test_control_plane_api.py` 기존 테스트는 `Authorization` 헤더를 추가하도록 수정 — 수정 전 401로 실패해야 함.

---

## 4. H2 — EGW Signed Context 부재

### 4.1 위협

- 공격자가 `X-Agent-Context: {"tenant_id":"acme","user_id":"employee:kim","agent_id":"agent:assistant:kim"}` 를 위조하여 EGW `POST /v1/execute` 로 타 테넌트 자원 접근. `tool_policy`·`authz_hook`이 위조된 컨텍스트로 정책 결정을 오염.
- 영향: tenant isolation 붕괴, capability token 검증 우회 (`context`가 위조되면 `verify_capability` 비교 대상이 오염).

### 4.2 현재 증거

- `execution-gateway/execution_gateway/app.py:242-272 _parse_agent_context_header` — `X-Agent-Context` 를 `json.loads` 또는 `base64` 디코드 후 그대로 `ctx`에 병합. 서명 검증 0.
- `app.py:280 _require_context` — `user_id` 없으면 401, 그 외는 `tenant_id=default` 로 보정. 위조 탐지 없음.
- `app.py:358 x_agent_context: str|None = Header(...)` — 평문 헤더만 받음, JWT 헤더 없음.
- `capability.py:34 verify_capability` 는 `context` 를 신뢰 — 위조된 `session_id`/`request_id` 로 토큰 재사용 공격 확장.

### 4.3 목표 불변식

1. **I-H2-1**: `prod` 에서 EGW는 `X-Agent-Context-JWT` (CP가 `OAOS_SIGNING_KEY`로 서명한 JWT) 만 신뢰. 평문 `X-Agent-Context`·개별 `X-*` 헤더는 `401 INVALID_CONTEXT` .
2. **I-H2-2**: `non-prod` 에서도 기본 검증은 동일하나 실패 시 `WARNING` 과 함께 401을 반환하며 평문 우회는 없음. `X-Agent-Context-JWT` 가 유일한 신뢰 경로.
3. **I-H2-3**: JWT의 `tenant_id`, `user_id`, `agent_id`, `session_id`, `trace_id`, `request_id` 는 EGW가 `aud=execution-gateway`, `iss=control-plane`, `exp` 검증 후 추출. `capability_token` 의 `session_id`·`request_id` 와 대조.

### 4.4 API/토큰 계약

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

# CP_SIGNED_JWT (AgentContext JWT)
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
  "jti":"uuid"
}
# 검증 실패
401 { "code":"INVALID_CONTEXT", "message":"missing or invalid X-Agent-Context-JWT" }
401 { "code":"CONTEXT_EXPIRED" }
403 { "code":"TENANT_MISMATCH", "message":"context tenant != resource tenant" }
```

- 서명: `HS256` (`OAOS_SIGNING_KEY`) — 차후 `RS256` + `JWKS` 로 확장 가능.
- CP는 세션 생성·prompt 전달 시 `to_agent_context()` (`control-plane/control_plane/session.py:44`) 를 JWT로 래핑하여 EGW로 전달.

### 4.5 마이그레이션

1. CP에 `issue_agent_context_jwt(rec: SessionRecord, request_id) -> str` 추가, `acp_adapter.py`·`app.py:277 send_prompt` 에서 EGW 호출 시 `X-Agent-Context-JWT` 헤더 부착.
2. EGW에 `verify_agent_context_jwt` 추가. 평문 경로는 prod에서 제거 — 코드는 JWT만 검증하며 평문으로는 인증되지 않음. shadow 검증(평문 요청을 401로 거부) 후 즉시 enforcement.
3. `deploy/k8s` ConfigMap에서 prod는 JWT 검증에 필요한 키만 포함, 평문 관련 키는 존재하지 않음.

### 4.6 롤백

- **prod 롤백(안전)**: 이전 **서명된** EGW 이미지로 `helm rollback execution-gateway <prev>` 또는 `kubectl rollout undo deploy/execution-gateway` — CP JWT 발급 장애 시 prod는 `503` fail-closed(평문 fallback 없음). 트래픽 드레인 또는 maintenance mode로 차단.

### 4.7 테스트 acceptance

- `tests/test_egw_signed_context.py` 7건: `test_plaintext_rejected_in_prod`, `test_valid_jwt_accepted`, `test_tenant_mismatch_403`, `test_expired_401`, `test_tampered_payload_401`, `test_capability_session_binding`, `test_nonprod_plaintext_rejected`.
- `execution-gateway` E2E: 평문 헤더로 요청 시 prod 401, JWT로 200.

---

## 5. H3 — Personal Wiki JWT / Ownership 검증 부재

### 5.1 위협

- Wiki vault 경로가 `get_vault_root() / f"{tenant}/{agent}/..."` 문자열 조합 (`vault.py:26-55` )으로만 결정되고, `execution-gateway`·`memory_service` 호출 시 소유자 증명이 없음. 공격자가 `tenant_id`/`agent_id`를 위조하면 타인의 `journal/notes/attachments` 열람·오염.
- `memory_service/app.py` 검색이 `tenant_id`/`agent_id`를 body에서 받되 JWT 스코프 검증이 없으면 cross-tenant recall.

### 5.2 현재 증거

- `packages/personal-wiki/personal_wiki/vault.py:26-55` — `get_vault_root()`는 env 또는 `~/.open-agent-os/wiki-vault`, 소유자 검증 없음. `vault_path(*parts)` 는 `Path(root)/parts` 단순 join — `..` 탈출 검증은 `vault.py`에 있으나 호출부 검증은 EGW에 위임.
- `packages/personal-wiki/personal_wiki/consolidate.py:44-73` — watermark도 동일 루트.
- `docs/personal-wiki-design.md:6.1`은 `owner isolation` 을 명시하지만 코드 증거는 EGW의 경로 검증 1곳뿐, JWT 스코프 없음.
- `memory_service/app.py` (HEAD 미표시, `personal-wiki-design.md:5.2` 참조) — `POST /v1/memories/search` 가 `tenant_id`/`agent_id`를 body로 받음, 토큰 검증 부재.

### 5.3 목표 불변식

1. **I-H3-1**: Wiki의 모든 R/W는 **검증된** `Wiki JWT` (`tenant_id`, `agent_id`, `scope=wiki:read|wiki:write`, `aud=memory-service|wiki-fs`, `iss=control-plane|security`) **검증 mandatory** 후, `JWT.tenant/agent == path tenant/agent` 일 때만 허용. prod에서는 JWT 없는 body `tenant_id` 신뢰 금지.
2. **I-H3-2**: 경로 정규화 후 `vault_root` 외부 탈출(`..`, symlink) 시 `403 PATH_TRAVERSAL`.
3. **I-H3-3**: `memory_service` 검색은 `JWT` 스코프로 `tenant/agent` 필터를 강제 — body의 `tenant_id`를 무시하고 JWT에서 추출.

### 5.4 API/토큰 계약

```http
POST /v1/memories/search HTTP/1.1
Host: memory-service:8003
Authorization: Bearer <WIKI_JWT>
Content-Type: application/json

{ "query":"계약서", "top_k":5 }
# 서버는 JWT에서 tenant/agent 추출, body tenant 무시
# WIKI_JWT
{
  "iss":"control-plane",
  "aud":"memory-service",
  "sub":"employee:kim",
  "tenant_id":"acme",
  "agent_id":"agent:assistant:kim",
  "scope":"wiki:read",
  "exp":1714000300,
  "jti":"uuid"
}
# FS 쓰기 (EGW 내부)
ensure_vault_dirs(vault_root, jwt=wiki_jwt)  # jwt 검증 후 경로 생성
```

- Vault FS API: `append_journal(tenant, agent, content, wiki_jwt)` — 함수 인자로 `wiki_jwt` 필수, 내부에서 `verify_wiki_jwt` 호출.
- `personal_wiki/vault.py` 에 `verify_wiki_jwt(token, expected_tenant, expected_agent)` 추가.

### 5.5 마이그레이션

1. CP가 세션/요청마다 `wiki JWT` 발급 (짧은 TTL 5분), EGW는 이를 `memory_service` 호출 시 `Authorization` 헤더로 전달.
2. `memory_service` 에 JWT 검증 middleware 추가. 기존 body `tenant_id` 경로는 제거 — 코드는 JWT에서만 tenant를 추출.
3. Vault FS 함수는 `wiki_jwt` 인자를 mandatory로 변경, prod에서는 항상 JWT 검증. 별도 완화 env는 없음.

### 5.6 롤백

- **prod 롤백(안전)**: 이전 **서명된** `memory_service`/`personal-wiki` 이미지로 `helm rollback` 또는 `kubectl rollout undo` — 필요 시 트래픽 차단(maintenance mode) 또는 read-only로 fail-closed.

### 5.7 테스트 acceptance

- `tests/test_personal_wiki_auth.py` 6건: `test_cross_tenant_read_403`, `test_cross_agent_read_403`, `test_path_traversal_403`, `test_valid_jwt_read_200`, `test_expired_jwt_401`, `test_body_tenant_ignored`.
- `personal_wiki` FS 단위: `vault_path("..","etc","passwd", wiki_jwt=...)` → 403.

---

## 6. H4 — Readiness fail-open (`200 degraded`)

### 6.1 위협

- DB/Redis 장애 시에도 `/readyz`가 `200 {status:degraded}` 를 반환하므로 K8s Endpoints에서 pod가 제외되지 않음. 트래픽이 degraded pod로 계속 유입되어 연쇄 실패, 롤링 배포 중에도 `maxUnavailable` 보장이 무력화.
- 현재 `docs/architecture-v1.7.0.md:1279` 는 `degraded여도 200 유지(fail-open)` 를 의도적으로 기술 — prod에서는 가용성보다 안전성(트래픽 차단)이 우선이어야 함.

### 6.2 현재 증거

- `control-plane/control_plane/app.py:196-203` — `return {status: degraded if degraded else ok, ...}` 항상 200.
- `execution-gateway/execution_gateway/app.py:324-327` — 동일, `_ha_checks()` 의 `_bounded_db_ping/_bounded_redis_ping` 실패가 `status:degraded` 로만 반영.
- `security/app.py:188-195` — 동일, `_ha_checks()` 는 `_check_latency` 로 `degraded` 만 기록.
- `tests/test_ha.py:44,50` — `assert r.status_code == 200  # fail-open always 200` — 테스트가 fail-open을 고정.
- `deploy/k8s` 의 `readinessProbe: {httpGet: {path:/readyz, port:8000}}` 는 200만 `Ready=True` 로 간주 — 200 degraded가 Ready로 오판.

### 6.3 목표 불변식

1. **I-H4-1**: `OAOS_ENV=production` 에서 `checks.db==degraded OR checks.redis==degraded` 이면 `/readyz` 는 `503 {status:degraded, ...}` — **prod에서는 503 mandatory, 완화 불가**. `non-prod` 에서는 `200 degraded` + `WARNING` 유지 (하위호환).
2. **I-H4-2**: `/healthz` (liveness)는 항상 200 — readiness와 분리. `K8s livenessProbe`는 `/healthz`, `readinessProbe`는 `/readyz` 를 각각 사용.
3. **I-H4-3**: `/_shutting_down==true` (SIGTERM 드레인) 시에도 prod `/readyz`는 `503 draining` .

### 6.4 API 계약

```http
GET /readyz HTTP/1.1
Host: control-plane:8000

# prod, DB degraded
HTTP/1.1 503 Service Unavailable
{ "status":"degraded", "service":"control-plane", "checks":{ "db":{"status":"degraded","latency_ms":820,"error":"db ping failed: timeout"}, "redis":{"status":"ok"}, "self":{"status":"ok"} } }

# non-prod, 동일 상황
HTTP/1.1 200 OK
{ "status":"degraded", ... }  # WARNING 로그 동반

# healthy
HTTP/1.1 200 OK
{ "status":"ok", ... }
```

- 구현: `app.py:readyz()` 에서 `if is_production() and degraded: return JSONResponse(status_code=503, content=...)`.

### 6.5 마이그레이션

1. 3-tier `readyz`에 `is_production()` 분기 추가, `tests/test_ha.py` 는 `prod` 케이스에 503을 기대하도록 분기.
2. `deploy/k8s/*.yaml` 의 `readinessProbe.failureThreshold` 유지, `periodSeconds:10` 로 30초 내 제외.
3. `docs/ha.md` 정본 업데이트 — `fail-open` 서술을 `non-prod` 한정으로 수정.

### 6.6 롤백

- **prod 롤백(안전)**: 이전 **서명된** 이미지로 `helm rollback <release> <prev>` 또는 `kubectl rollout undo deploy/<svc>` — 트래픽은 `readiness 503` 으로 자동 차단되며, 긴급 시 ingress `maintenance mode` 로 명시적 차단. prod에서 완화 경로는 없음.

### 6.7 테스트 acceptance

- `tests/test_ha.py` 4건 수정: `test_readyz_prod_strict_503_on_db_fail`, `test_readyz_nonprod_200_degraded`, `test_readyz_healthy_200`, `test_liveness_always_200`.
- `kind` 검증: `DATABASE_URL=postgresql://invalid:5432` 로 pod 기동 후 `kubectl get endpoints` 에서 제외됨을 `curl -w %{http_code}` 로 증명.

---

## 7. H5 — Distributed quota/state per-replica

### 7.1 위협

- Tenant quota(`daily 100, per-minute 10`)가 `llm_runtime.py:433 _llm_quota_store={}` 및 `admin-console/backend/llm_providers.py:946 _quota_store` in-memory dict로 레플리카별 로컬. 3 replica 시 공격자가 라운드로빈으로 300/30 호출 가능.
- Session(`InMemorySessionStore`)도 레플리카별 — sticky session 없이 요청이 다른 pod로 가면 `session not found`.
- Token nonce/jti(`security/token/service.py:42 _seen_nonces=set()`)도 in-memory — 동일 토큰을 다른 레플리카에서 replay 가능.

### 7.2 현재 증거

- `packages/agent-runtime/agent_runtime/llm_runtime.py:429-569` — 주석 `TODO(distributed): quota state is per-replica` 명시. `*_quota_store`·`_llm_quota_window_counts` 전역 dict, DB 경로는 `CREATE TABLE IF NOT EXISTS admin_llm_quotas` 후 `fail-open` fallback.
- `control-plane/control_plane/session.py:88-140 InMemorySessionStore` — 기본 `session_store = InMemorySessionStore()` (`session.py:285`). `RedisSessionStore` 는 특정 조건에서만, 그것도 `fallback=True` 로 in-memory fallback이 혼재.
- `security/token/token_service/service.py:88-180` — prod에서 Redis primary를 시도하나 `REDIS_URL` 미설정 시 `RuntimeError` 후 `non-prod` fallback 흐름이 혼재. `packages/personal-wiki`·`execution-gateway/tool_policy.py` 의 `ToolRateLimiter` 도 Redis Lua 미사용 시 in-memory.
- `docs/architecture-v1.7.0.md:1189-1195` — quota가 `DB+in-memory 이중` 으로 기술, 분산 원자성 없음.

### 7.3 목표 불변식

1. **I-H5-1 (quota)**: `prod` 에서 quota는 **Redis Lua 원자 증가+윈도 체크**가 primary — **구현·kind 2-replica 통합 검증 완료 전까지 문서/코드에서 원자성 보장 주장 금지, 현재는 per-replica in-memory(비원자)임을 명시**. prod에서 in-memory 경로는 제거되며 Redis 미가용 시 `503 QUOTA_BACKEND_UNAVAILABLE` fail-closed (현재 `llm_runtime.py:523-543` 의 prod 503 분기는 유지·확대). 매일/분 단위 Lua는 `INCR+EXPIRE` 원자.
2. **I-H5-2 (session)**: Session은 `prod`에서 `RedisSessionStore` mandatory — 기본 `fallback=False`, **session state는 Redis에 영속, in-memory는 non-prod만**. `InMemory` 는 `non-prod`만. **세션 생성/조회의 원자성은 Redis `SET NX`/`GET` + TTL로만 보장, in-memory 주장 금지**.
3. **I-H5-3 (replay)**: Token replay는 `Redis SET NX ex=ttl` 로 분산 원자성. `in-memory set`은 `non-prod` 보조만. **원자성 주장은 Redis 구현 머지 후에만**.
4. **I-H5-4 (rate)**: Tool rate-limit(`execution-gateway/tool_policy.py`)도 **Redis Lua token-bucket/sliding-window 원자** primary. prod에서 Redis 미가용 시 503 또는 `429 RATE_BACKEND_UNAVAILABLE` fail-closed, `_Noop allow=True` 금지.
5. **I-H5-5 (no claim)**: **모든 Redis Lua/SET NX 원자성 주장은 `kind 2-replica + Redis` 통합 테스트·`redis-cli` 캡처·`k6` 병렬 검증을 통과하기 전까지 문서/릴리즈 노트에서 “원자성 보장됨”으로 표기 금지. 미검증 상태에서 `distributed: N passed` 를 0으로 유지.**

### 7.4 API/토큰 계약

**Quota Redis 키**:

```
oaos:quota:{tenant_id}:daily:{YYYY-MM-DD}  -> INCR, EXPIRE 86400
oaos:quota:{tenant_id}:minute:{YYYY-MM-DDTHH:MM} -> INCR, EXPIRE 120
```

**Lua (원자)**:

```lua
local daily = KEYS[1]; local minute = KEYS[2]
local dlim = tonumber(ARGV[1]); local mlim = tonumber(ARGV[2])
local dc = redis.call('INCR', daily); if dc==1 then redis.call('EXPIRE', daily, 86400) end
local mc = redis.call('INCR', minute); if mc==1 then redis.call('EXPIRE', minute, 120) end
if dc > dlim then return {-1, dc, mc} end
if mc > mlim then return {-2, dc, mc} end
return {0, dc, mc}
```

- `llm_runtime.py:_llm_quota_check` 는 Lua 반환 `-1`→`429 daily`, `-2`→`429 per-minute`, `0`→ 통과. `RedisTimeout`→ prod 503.

**Session**:

```python
# prod factory — Redis mandatory, fallback 없음
session_store = create_session_store(backend="redis", redis_url=REDIS_URL, fallback=False, ttl_seconds=86400)
# non-prod — InMemory 허용
session_store = InMemorySessionStore()
```

**Token**: 이미 `security/token/service.py:145-180` 의 `SET NX ex=ttl` 로직을 정본으로, `prod`에서 `r is None` 이면 즉시 503.

### 7.5 마이그레이션

1. `admin-console/backend/llm_providers.py` 및 `llm_runtime.py` 의 in-memory 경로를 `if is_production(): raise 503` 로 가드, Redis Lua로 교체. prod 이미지에서는 in-memory 분기를 제거.
2. `control-plane` Deployment에 `REDIS_URL` 주입, `RedisSessionStore(fallback=False)` 로 전환. 기존 `InMemory` 세션은 TTL 24h 후 자연 소멸 — 마이그레이션 스크립트 불필요.
3. `security/token` 은 `REDIS_URL` 필수 검증 이미 존재 (`_require_redis_if_prod`) — 유지, prod에서 in-memory fallback은 없음.

### 7.6 롤백

- **prod 롤백(안전)**: Redis 장애 시 **503 fail-closed 유지** — 복구는 **Redis 의존성 복구**(Sentinel/Cluster failover, `helm rollback redis` 로 이전 서명된 Redis 리비전 복구) 또는 이전 **서명된** 앱 이미지로 `helm rollback` . 트래픽은 readiness 503 또는 ingress `maintenance mode`/`read-only` 로 자동 차단. prod에서 in-memory로 완화하는 롤백은 없음.
- 긴급 시 트래픽 드레인 또는 전체 중지로 fail-closed 유지.

### 7.7 테스트 acceptance

- **unit**: `tests/test_llm_quota.py` 에 `test_quota_distributed_atomic` — `fakeredis` 또는 `redis` 목으로 Lua 원자성 검증, `test_quota_prod_503_on_redis_down`.
- **distributed**: `kind` 2 replica + `redis` 1개로 `k6` 또는 `pytest-xdist` 병렬 30 req → `429` 가 정확히 10개 이후 발생함을 `redis-cli GET oaos:quota:*:minute:*` 로 증명. `session`은 replica A에서 생성 후 replica B에서 `GET /v1/sessions/{id}` 200.
- **evidence**: `redis-cli --raw SCAN` 캡처, `pytest -k test_quota_distributed`.

---

## 8. H6 — NetworkPolicy enforcement 미검증

### 8.1 위협

- `deploy/k8s/networkpolicy.yaml` 8종이 `kind: NetworkPolicy` 로 존재하나, CNI가 `default-deny` 를 실제로 enforce 하는지, `Cilium Hubble`/`Calico flow` 로그가 audit으로 수집되는지 증거 없음. `allow-postgres` 등이 `app: postgres` 라벨에만 의존 — 라벨 오기재 시 deny.
- 영향: 랜터럴 무브먼트, `hermes` pod가 `postgres:5432` 로 직접 접근 시 차단되지 않음, `deny-audit` 가 실제 로그 없이 문서상 존재.

### 8.2 현재 증거

- `deploy/k8s/networkpolicy.yaml:1-250` — `default-deny-all` (podSelector:{}), `allow-dns-egress`, `allow-postgres`, `allow-redis`, `allow-control-plane`, `allow-egress-*`, `deny-audit` 7+N. `deny-audit` 는 `spec:{}` deny-all을 중복 정의, 실제 audit 주석만 존재.
- `deploy/firewall/hermes-egress.nft` — `hermes` uid 기반 nft, K8s NetworkPolicy와 이중 관리, 동기화 검증 없음.
- `docs/architecture-v1.7.0.md:1401` — `NetworkPolicy 8종 default-deny+audit` 로 기술하나, `docs/deployment-verification-2026-08-27.md` 는 수동 `kubectl apply` 로그만.
- CI: `kubeconform`·`kind`·`cilium connectivity test` 없음. `tests/`에 NetworkPolicy 단위 테스트 0.

### 8.3 목표 불변식

1. **I-H6-1**: `default-deny-all` 적용 후, `allow-*` 에 명시되지 않은 `ingress/egress` 는 `DROP` + `audit log` 로 기록. `hermes`→`postgres:5432` 직접 시도는 `DROP` .
2. **I-H6-2**: `CNI` (Cilium 권장)는 `hubble` 또는 `flow logs` 로 `verdict=DROPPED` 를 `Loki/SIEM` 으로 전달. `deny-audit` 는 NetworkPolicy가 아닌 `CiliumNetworkPolicy` 또는 `CNI config` 로 대체.
3. **I-H6-3**: CI에서 `kind` 클러스터에 `networkpolicy.yaml` 적용 후 `agnhost`/`netcat` pod로 `default-deny` **실제 CNI enforcement** 차단을 검증 — YAML 존재만으로 enforcement 주장 금지, `hubble/flow log` 또는 `connectivity test` 캡처가 증거.

### 8.4 API/계약 (K8s)

```yaml
# CiliumNetworkPolicy 예시 (audit)
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata: {name: audit-deny, namespace: open-agent-os}
spec:
  endpointSelector: {}
  ingress: [{fromEntities: [cluster], toPorts: [{ports: [{port: "8000", protocol: TCP}]}]}]
  egress: [{toEndpoints: [{matchLabels: {app: postgres}}], toPorts: [{ports: [{port: "5432"}]}]}]
# hubble
# cilium hubble --verdict DROPPED --since 1m
```

- `deploy/k8s/networkpolicy.yaml` 은 `networking.k8s.io/v1` 유지하되, `deny-audit` 는 `CiliumNetworkPolicy` 로 분리 또는 삭제. `allow-control-plane` 의 `ingress.from` 에 `namespaceSelector` 추가 (ingress-nginx 네임스페이스 명시).

### 8.5 마이그레이션

1. `deploy/k8s/networkpolicy.yaml` 정리: `deny-audit` 문서 주석을 `CiliumNetworkPolicy` 매니페스트로 승격, `allow-*` 라벨을 `app.kubernetes.io/name` 로 표준화.
2. `helm`/`kustomize` 로 `CNI` 선택 (Cilium) — `values.yaml` 에 `cni.cilium.hubble.enabled=true`.
3. `deploy/scripts/verify-network-policy.sh` 추가 — `kind create cluster --config kind-cni.yaml`, `kubectl apply -f networkpolicy.yaml`, `kubectl run tester --image=busybox --command -- nc -zv postgres 5432` → `DROP` 확인. CNI 미지원 kind에서는 NetworkPolicy를 적용하지 않음.

### 8.6 롤백

- **prod 롤백(안전)**: 이전 **서명된** NetworkPolicy/CiliumNetworkPolicy 리비전으로 `helm rollback <release> <prev>` — 긴급 시 CNI 설정 복구 또는 ingress `maintenance mode`/`read-only` 로 트래픽 차단. 전체 개방으로 롤백하는 경로는 없음.

### 8.7 테스트 acceptance

- `tests/test_network_policy.py` (kind) — `test_default_deny_blocks_hermes_to_postgres`, `test_allow_postgres_from_control_plane`, `test_dns_egress_allowed`.
- CI `verify-network-policy.sh` 가 `kind` 에서 `PASS 3/3` 로 종료, `hubble --verdict DROPPED` 로그 캡처.

---

## 9. H7 — Mock/Fallback prod 차단 불완전

### 9.1 위협

- `llm_runtime.py:901 _is_mock_allowed` 가 prod에서도 mock을 허용할 수 있음. `packages/agent-runtime/agent_runtime/providers/*` 의 `if OAOS_ENV==production` 가드는 provider별 분산, 누락 시 mock fallback.
- `execution-gateway/execution_gateway/app.py:230 _Noop limiter` — `ToolRateLimiter` import 실패 시 `allow()=True` noop으로 전체 rate limit 우회.
- 영향: prod에서 외부 LLM 장애 시 민감 데이터가 mock 경로로 유출 없이 실패해야 하나, mock이 실제 응답처럼 반환되어 업무 오류·감사 누락.

### 9.2 현재 증거

- `packages/agent-runtime/agent_runtime/env_gate.py:28 is_mock_allowed` — 현재 prod 기본은 `False` 이나 구현이 env에 따라 분기.
- `llm_runtime.py:1530-1532` — `if not _is_mock_allowed(): raise RuntimeError("mock fallback disabled")` — 분기가 존재.
- `mcp_client.py:37-60` — `_is_production()` 복사본, `gateway_unreachable` 시 `non-prod` fallback 흐름이 혼재.
- `execution-gateway/app.py:204-230 _get_rate_limiter` — `except Exception: class _Noop: allow=True` — import 실패가 곧 rate limit 무력화.

### 9.3 목표 불변식

1. **I-H7-1**: `prod` 는 **immutable startup gate** — `mock/fallback/noop` 코드 경로는 prod 이미지 빌드 시 제거되거나 기동 시 무조건 503 fail-closed. prod에서는 어떤 조건으로도 mock을 활성화할 수 없음.
2. **I-H7-2**: `prod`에서 `llm_runtime`·`mcp_client`·`rate_limiter` 의 mock/noop 분기는 `503` 또는 `RuntimeError` 로 fail-closed — 재시작/런타임 변경으로도 우회 불가. `non-prod`만 `WARNING` 후 fallback.
3. **I-H7-3**: `CI`에서 `OAOS_ENV=production` 으로 기동 시에도 `test_mock_blocked_in_prod` 가 503을 검증.

### 9.4 API/계약

```python
# env_gate.py (정본) — prod immutable gate
def is_mock_allowed() -> bool:
    if is_production():
        return False  # prod: immutable — mock 경로는 이미지/기동 시 제거되어 503
    # non-prod에서만 허용
    return True  # non-prod 기본 허용 — 별도 완화된 env 키 없이 코드로 결정

# prod Helm/manifest/코드에서 mock 관련 완화 키는 존재하지 않음
```

- `execution-gateway/tool_policy.py` — `ToolRateLimiter` import 실패 시 prod는 `raise RuntimeError("rate limiter unavailable in prod")` , non-prod만 `_Noop`.

### 9.5 마이그레이션

1. `env_gate.py` 를 prod-무조건-`False` 로 정본화, `llm_runtime.py`·`mcp_client.py`·`execution-gateway/app.py` 의 `is_mock_allowed` 호출부를 정본으로 교체 (복사본 삭제). prod 이미지에서는 mock 분기 자체를 제거.
2. prod Helm/manifest에는 mock 관련 완화된 env가 존재하지 않음 — 코드 레벨에서 제거.
3. CI에서 prod manifest에 mock 관련 env가 존재하면 배포 차단 — prod manifest 린트에서 차단. `OAOS_ENV=production` 에서도 `is_mock_allowed()`가 `False` 임을 단위 테스트로 증명.

### 9.6 롤백

- **prod 롤백(안전)**: 이전 **서명된** 이미지로 `helm rollback <release> <prev>` 또는 `kubectl rollout undo deploy/<svc>` — 필요 시 `maintenance mode` 또는 트래픽 드레인으로 차단.

### 9.7 테스트 acceptance

- `tests/test_mock_fallback_hardening.py` 5건: `test_prod_mock_blocked`, `test_nonprod_mock_allowed`, `test_rate_limiter_noop_blocked_in_prod`, `test_mcp_gateway_unreachable_503_in_prod`, `test_mock_path_removed_from_prod_image`.
- `pytest -k test_mock_fallback_hardening` 5 passed, `OAOS_ENV=production` 에서 `curl` 로 mock 응답이 아닌 503 캡처.

---

## 10. H8 — Test evidence/docs 과장 (외부/분산 미검증)

### 10.1 위협

- `README.md:179`, `docs/architecture-v1.7.0.md:1373` 이 `648 tests passed` 를 단일 숫자로 제시, 분산/외부/네트워크 검증은 `skipped` 또는 `in-memory` 로 통과. 운영자는 prod 분산 신뢰성을 과대 평가.
- `docs/deployment-verification-2026-08-27.md` 5줄, `docs/deployment.md` 5줄 — 배포 검증 문서가 형식적.

### 10.2 현재 증거

- `pytest 648 passed` — `tests/test_ha.py:44` `status in (ok,degraded)`, `security/app.py:196-201` `no DATABASE_URL → skipped`, `control-plane/session.py:285` `InMemorySessionStore` 기본.
- `docs/architecture-v1.7.0.md:1389-1405` — `TODO distributed`, `Vault encrypted_postgres legacy`, `readiness 200+degraded`, `env gate mirror drift`, `Redis HA 필요` 를 `Residual` 로 명시하나, README 전면에는 `648` 만 노출.
- `docs/GAP_AUDIT_CORE_PLATFORM_v1.6.md` 210줄 — gap 감사 문서이나 v1.6 이후 업데이트 없음.

### 10.3 목표 불변식

1. **I-H8-1**: 증거 등급 분리 — `unit: N passed`, `integration: N passed`, `distributed: N passed (Redis/Kind/Cilium)`, `external: N passed (Mattermost/Slack/LLM Gateway)` 로 README 표기. 단일 `648` 은 **현재 `unit+integration` 만**으로 한정하며, **distributed/external은 구현·검증 완료 후에만 별도 카운트**.
2. **I-H8-2**: `Residual` (TODO distributed/Vault legacy/readyz 200) 해소 전에는 문서 상단 `Warning: distributed quota not enforced` 배너 유지.
3. **I-H8-3**: 배포 검증은 `kind` + `helm` + `k6` 로그를 `docs/deployment-verification-*.md` 에 첨부, `kubectl`·`curl`·`hubble` 캡처 포함.

### 10.4 계약 (문서/증거)

```markdown
# README 증거 표
| 등급 | 명령 | 결과 | 증거 |
|------|------|------|------|
| unit | `pytest -q` | 612 passed | CI log #123 |
| integration | `pytest -k test_e2e` | 36 passed | CI log #123 |
| distributed | `kind + redis` `pytest -k distributed` | 12 passed | kind log, redis-cli SCAN |
| external | `mattermost acp llm mcp` E2E 4건 | 4 passed | `tests/test_e2e_mattermost_acp_llm_mcp.py` |
| 합계 |  | 664 |  |
```

- `docs/architecture-v1.7.1.md` 헤더에 `Residual` 배지, 해소 시 제거.

### 10.5 마이그레이션

1. `README.md`·`SECURITY.md`·`docs/architecture-v1.7.1.md` 헤더를 등급 분리 표로 교체, 기존 `648` 은 `unit+integration` 로 재정의.
2. `Makefile` 에 `make verify-distributed` (kind+redis) 타겟 추가, CI `verify-distributed` job 신설.
3. `docs/deployment-verification-2026-08-29.md` 를 80줄 이상으로 확장 — `kubectl`, `curl /readyz`, `redis-cli`, `hubble` 캡처 포함.

### 10.6 롤백

- 문서 롤백은 `git revert` — 기능 영향 없음.

### 10.7 테스트 acceptance

- `README` 에 `distributed` 등급이 0이면 CI `fail`. `make verify-distributed` 가 `kind` 없이 `SKIP` 이 아닌 `FAIL` 로 처리 (증거 강제).
- `docs/deployment-verification-*.md` 가 50줄 미만이면 `ci/docs-lint` 실패.

---

## 11. 공통 마이그레이션·롤아웃 순서 (권장)

```text
Phase 0 — 정본화 (1일) — 단일 env_gate 패키지와 CI
  - 단일 정본 패키지 확립: packages/env-gate (또는 packages/agent-runtime/env_gate.py 를 정본으로 단일화)
  - 모든 서비스에서 import env_gate.is_production() 만 사용, mirror drift 제거
  - CI: grep -r "OAOS_ENV.*production" --exclude=env_gate.py 시 fail, helm lint에서 prod 완화 키 존재 시 fail
  - docs/architecture-v1.7.1-design.md 머지, README Residual 배너 추가
  - 현재 상태: env_gate 분산, readiness 200, 평문 context, in-memory quota — 목표와의 차이를 Residual로 명시

Phase 1 — 인증/서명 원자 배포 (2~3일) — C1+H1+H2+H3
  - Security auth (C1) + CP JWT (H1) + EGW JWT (H2) + Wiki JWT (H3) 를 단일 릴리스로 원자 배포
  - shadow 검증: 새 코드에서 서명 검증을 수행하고 검증 실패를 메트릭/로그로 기록하되, 초기 1~2시간 관찰 후 즉시 enforcement(401)로 전환
  - prod에서 완화 경로는 없음 — 코드 레벨에서 평문 경로는 제거되며, 배포는 서명된 이미지로만 진행
  - 검증: tests/test_security_auth, test_cp_identity, test_egw_signed_context, test_personal_wiki_auth 녹색

Phase 2 — Redis/DB primary와 readiness (2일) — H5+H4
  - Redis Lua quota / RedisSessionStore(fallback 없음) / SET NX replay (H5) 를 prod primary로 전환 — Redis 미가용 시 503
  - readiness strict 503 (H4) 적용 — prod에서 degraded 시 503으로 트래픽 차단
  - kind 2-replica + Redis 통합 테스트, k6 병렬 quota 검증, redis-cli SCAN 캡처

Phase 3 — NetworkPolicy CNI 증명 (1~2일) — H6
  - NetworkPolicy 정리 및 CiliumNetworkPolicy 승격, CNI(hubble/flow) audit 연동
  - kind + CNI로 default-deny 실제 차단 검증 — YAML 존재만으로 enforcement 주장 금지
  - verify-network-policy.sh CI 연동, hubble --verdict DROPPED 캡처

Phase 4 — mock/evidence (1일) — H7+H8
  - mock/fallback/noop prod immutable gate — prod 이미지에서 mock 경로 제거, prod에서 항상 503
  - README 등급 분리, deployment-verification 확장, Makefile verify-distributed, 증거 캡처
  - prod에서 완화 플래그는 존재하지 않음

각 Phase는 서명된 이미지/Helm 리비전 롤백, 트래픽 드레인/maintenance/read-only, 의존성 복구, 또는 전체 중지로만 롤백 — prod 완화는 없음.
prod 배포 순서: kind prod dry-run → staging → prod 롤아웃.
```

---

## 12. 전체 롤백 전략 (prod 안전 — 완화된 env/개방 없음)

| 트리거 | 롤백 명령 (prod 안전) | 영향 | 감사 |
|--------|-----------|------|------|
| Security 401 대량 발생 | `helm rollback security <prev>` 또는 `kubectl rollout undo deploy/security` (서명된 이전 이미지) + ingress `maintenance mode` 또는 `read-only` 로 트래픽 차단 | 1~2분 | `audit_log`에 `rollback:security_helm` |
| CP JWT 검증 실패 | `helm rollback control-plane <prev>` 또는 `kubectl rollout undo deploy/control-plane` (서명된 이전 이미지) + `maintenance mode` | 1~2분 | `rollback:cp_helm` |
| EGW 401 대량 | `helm rollback execution-gateway <prev>` 또는 `kubectl rollout undo deploy/execution-gateway` (서명된 이전 이미지) + 트래픽 드레인 | 1~2분 | `rollback:egw_helm` |
| readiness 503으로 트래픽 0 | `helm rollback <svc> <prev>` (서명된 이전 이미지)로 복구. 503은 정상 fail-closed이므로 **의존성 복구**(DB/Redis 복구)가 우선, 트래픽은 `maintenance mode` 로 제어 | 2~5분 | `rollback:readiness_helm` |
| quota 503 Redis 장애 | **Redis 복구 우선**: `helm rollback redis <prev>` / Sentinel failover / `REDIS_URL` 복구 (의존성 복구). 트래픽은 readiness 503 또는 `maintenance mode` 로 자동 차단 | 5분 | `rollback:quota_redis_restore` |
| NetworkPolicy DROP 과다 | `helm rollback <release> <prev>` 로 이전 서명된 NetworkPolicy/CiliumNetworkPolicy 리비전 복구. 긴급 시 CNI 설정 복구 또는 ingress `maintenance mode`/`read-only` 로 차단 | 1분 | `rollback:netpol_helm` |
| mock 차단 오탐 | `helm rollback <svc> <prev>` (서명된 이전 이미지) + `maintenance mode` 로 트래픽 제어 | 1~2분 | `rollback:mock_helm` |

- 모든 prod 롤백은 `helm rollback <release> <prev>` 또는 `kubectl rollout undo` (서명된 이전 이미지), 트래픽 드레인/`maintenance mode`/`read-only`, 의존성 복구(Sentinel/Redis/DB 복구), 또는 전체 중지 중 하나로만 수행 — **prod에서 완화하는 롤백은 없음**.
- DB 마이그레이션은 v1.7.1에서 없으므로( quota 테이블은 v1.6.4에서 이미 존재) 스키마 롤백 불필요.
- 롤백 후 `docs/deployment-verification-rollback-*.md` 에 사유·시각·영향 기록.

---

## 13. 테스트 맵 (v1.7.1 acceptance 전체)

| 항목 | 테스트 파일 | 건수 | 비고 |
|------|-------------|------|------|
| C1 | `tests/test_security_auth.py` | 6 | prod 401, JWT/exp/aud/mTLS |
| H1 | `tests/test_cp_identity.py` | 5 | sub mismatch, owner isolation |
| H2 | `tests/test_egw_signed_context.py` | 7 | plaintext reject, tenant binding |
| H3 | `tests/test_personal_wiki_auth.py` | 6 | traversal, cross-tenant |
| H4 | `tests/test_ha.py` (수정) | 4 | prod 503, non-prod 200 |
| H5 | `tests/test_llm_quota.py` + `test_session_distributed.py` + `test_token_distributed.py` | 8 | fakeredis, kind 2-replica |
| H6 | `tests/test_network_policy.py` (kind) | 3 | agnhost nc, hubble |
| H7 | `tests/test_mock_fallback_hardening.py` | 5 | immutable gate |
| H8 | `ci/docs-lint` + `make verify-distributed` | 2 | 등급 분리, 50줄 gate |
| **합계** |  | **46** | 기존 648 + 46 = 694 (목표) — distributed 12는 kind 필요 |

- 기존 `tests/test_control_plane_api.py`, `test_e2e_mattermost_acp_llm_mcp.py` 는 JWT 헤더 추가로 수정 — 수정 전 401 실패가 정상.

---

## 14. 파일·코드 변경 목록 (구현 시)

```
packages/env-gate/env_gate.py          # 신설 또는 agent-runtime/env_gate.py 정본화 — 단일 정본, CI drift 차단
security/app.py                          # verify_security_auth dependency, mTLS
control-plane/control_plane/app.py       # verify_user_jwt, issue_agent_context_jwt
control-plane/control_plane/session.py   # RedisSessionStore fallback=False prod
control-plane/control_plane/acp_adapter.py # X-Agent-Context-JWT 부착
execution-gateway/execution_gateway/app.py # verify_agent_context_jwt, strict readyz
execution-gateway/execution_gateway/tool_policy.py # prod noop 금지
packages/personal-wiki/personal_wiki/vault.py # verify_wiki_jwt, PATH_TRAVERSAL
memory_service/app.py                    # Wiki JWT middleware
packages/agent-runtime/agent_runtime/llm_runtime.py # Redis Lua quota, prod 503
packages/agent-runtime/agent_runtime/mcp_client.py # env_gate 정본 import
admin-console/backend/llm_providers.py   # Redis Lua quota
security/token/token_service/service.py  # prod Redis mandatory 유지, 문서화
deploy/k8s/networkpolicy.yaml            # CiliumNetworkPolicy 분리
deploy/k8s/configmap.yaml                # prod 완화 키 없음 — 서명에 필요한 키만
deploy/scripts/verify-network-policy.sh  # 신설
Makefile                                 # verify-distributed
docs/architecture-v1.7.1.md              # 5026→ ~5400줄, Residual 갱신
README.md / SECURITY.md                  # 증거 등급 분리
```

---

## 15. 문서·증거 갱신

- `docs/architecture-v1.7.1.md` — 본 설계안을 반영한 정본. `v1.7.0` 5026줄 대비 `+~400줄`. 헤더에 `Residual` 배지 유지, 해소 시 제거. 현재 상태(HEAD 932e558868: in-memory, 200 degraded, 평문 context)가 목표와 어떻게 다른지 명시.
- `docs/deployment-verification-2026-08-29.md` — kind/helm/k6/hubble 캡처 80줄 이상.
- `docs/security-review-gateway-bypass.md` — H2 해소 후 `gateway bypass` 시나리오 재검증.
- `docs/ARCHITECTURE_DECISIONS.md` — ADRs: `ADR-014 Security API auth`, `ADR-015 Signed AgentContext`, `ADR-016 Readiness strict`, `ADR-017 Distributed quota`.

---

## 16. 잔여 리스크 및 비구현 항목

- `RS256` 로테이션·JWKS는 v1.7.1에서 설계만, 구현은 v1.8.
- `Vault encrypted_postgres` legacy 마이그레이션은 본 설계에서 `wiki JWT` 로 격리만, 실제 암호화 백엔드 교체는 별도 `vault-externalization` 트랙.
- `Redis HA` (Sentinel/Cluster)는 H5의 전제 — `deploy/k8s/redis-ha.yaml` 은 별도 설계.

---

*끝 — 본 문서는 HEAD 932e558868 기준 증거로 작성되었으며, 구현 전 2차 검증을 전제로 한다. 모든 prod 롤백은 서명된 이미지/Helm 리비전, 트래픽 드레인/maintenance/read-only, 의존성 복구, 또는 전체 중지로만 수행되며 완화된 env나 개방으로 롤백하지 않는다.*
