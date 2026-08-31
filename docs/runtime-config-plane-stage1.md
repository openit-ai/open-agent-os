# Runtime Configuration Plane — Stage 1 Minimal Vertical Slice

Status: Stage-1 implemented (2026-08-31)
Scope: versioned / signed canonical config snapshot without full hot-reload of all subsystems.

## 1. 기존 DB·API·P0/P1 호환성 분석 (read-only)

### Admin DB 현재 상태
- Tables (verified via `admin-console/backend/persistence.py` + `security/models/orm.py`):
  `admin_users`, `admin_infra_services`, `admin_user_mappings`, `admin_llm_providers`,
  `admin_policy_versions`, `admin_settings` (generic K/V), `admin_llm_quotas`, `admin_llm_usage`.
- Data counts (운영 가정): infra 8, mapping 1, users 1, providers 0 — snapshot sampling 시 참조 가능.
- Persistence pattern: 각 모듈(`auth.py`, `infra.py`, `user_mappings.py`, `llm_providers.py`,
  `runtime_mode.py`, `fallback.py`)은 **sync SQLAlchemy + admin_settings K/V**와
  in-memory dict 이중화를 사용. DB URL 없을 때 in-memory fallback, `OAOS_ENV=production`
  일 때 fail-closed (`ensure_admin_tables` + 각 모듈의 production guard).

### P0 / P1 커밋 보호
- P0 `00a6fcb890`: `control-plane/control_plane/idempotency.py`, `app.py`, `acp_adapter.py`,
  `session.py`, `internal_api.py` — idempotency + multimodal delivery. 테스트: `test_p0_idempotency.py`.
- P1 `57e9a4fcc2` / `8855441b83`: `knowledge_index/health.py`, `connectors/__init__.py`,
  `scripts/verify-knowledge-live.py` — live knowledge index + fail-closed Notion adapter.
- **금지 행위**: 이 파일들 수정 금지, dirty worktree 보존, 무관 파일 변경 금지.

### Stage-1 마이그레이션 필요성 판단
| 후보 | 판단 | 근거 |
|------|------|------|
| 새 테이블 `admin_runtime_config_snapshots` (versioned history, signature, published pointer, rollback, audit) | **장기적으로 필요** — durable versioned config, rollback pointer, applied state를 정규화 | `admin_policy_versions` 패턴과 동일 (id, tenant_id, version, status, rules_json, parent_version). Stage-2에서 정식 Alembic `015_runtime_config_snapshots.py` 로 승격 |
| Stage-1 실행 | **테이블 없이 진행** — `admin_settings` K/V + in-memory로 최소 슬라이스 구현 | 운영 지시: *운영 서비스 재기동·DB 변경 금지*. K/V 키 `runtime_config:snapshot:<tenant>:<version>`, `runtime_config:published:<tenant>` 로 history/publish/rollback을 구현하면 DDL 없이 검증 가능. P0/P1에 영향 없음. 테스트는 sqlite/in-memory로 통과 |
| DDL 초안 | 제공하되 실행하지 않음 (본 문서 §5) | 부모 승인·백업·재기동 게이트 후에만 `alembic upgrade` |

**결론**: Stage-1은 `admin_settings` + in-memory로 동작하며, 새 테이블 DDL은 문서/코드에 준비만 하고 DB에는 적용하지 않는다.

## 2. 설계 (Stage-1 최소 슬라이스)

### 저장 대상 (참조 버전만, secret 원문 금지)
- `runtime_mode` (hermes|llm) — `runtime_mode.get_mode()`
- `hermes` — `control_plane.config.settings.hermes_base_url` + `hermes_model` (CP settings 미러)
- `llm_providers` — enabled 목록만, 필드: `id, provider, name, model, base_url, path, url, enabled, secret_ref, vault_backend` — `encrypted_api_key`/plain `apiKey` 미포함
- `fallback` — `fallback._load_config()`의 `chain, enabled, fallback_model`
- `infra` — `infra.list` count + `content_hash` (sha256 of ids) + services는 `id,name,host,port,health_path,expected_status,status` (secret 없음)
- `user_mappings` — count + hash + `id, mm_user_id, employee_principal, agent_id` (display_name/avatar_url은 선택)

### Canonical snapshot
```json
{
  "tenant_id": "default",
  "version": 1,
  "created_at": "2026-08-31T00:00:00Z",
  "created_by": "admin@openit.co.kr",
  "published": false,
  "published_at": null,
  "published_by": null,
  "rollback_from": null,
  "parent_version": null,
  "config": { "runtime_mode": "...", "hermes": {...}, "llm_providers": [...], "fallback": {...}, "infra": {...}, "user_mappings": {...} },
  "signature": "hmac-sha256 hex"
}
```
- Canonical JSON: `json.dumps(config, sort_keys=True, separators=(',',':'))` → HMAC-SHA256 with `OAOS_RUNTIME_CONFIG_SIGNING_KEY` > `ADMIN_JWT_SECRET` > dev (prod fail-closed).
- `tenant/user scope`: key는 `tenant_id` (기본 `default`), 향후 `user_id` 확장. 조회 시 JWT tenant와 일치 검증.
- `optimistic version`: `POST /snapshot` 시 `expected_version` (헤더 `If-Match` 또는 body) 가 현재 `max_version+1` 과 다르면 `409 CONFLICT`.
- `rollback pointer`: `POST /rollback` 은 `published:<tenant>` 를 기존 history의 특정 version으로 이동 (`rollback_from` 기록, 새 스냅샷 생성 없음).
- `applied_by/applied_at/process identity`: Admin은 `created_by/published_by/published_at` 기록. CP는 적용 시 `applied_by=X-User-Id` + `applied_at` + `process_identity=hostname:pid` 를 `/v1/runtime-config/status` 로 반환.
- `fail-closed`: `OAOS_ENV=production` 에서 signing key가 dev 기본값이면 503, DB URL 없으면 snapshot publish 503, signature 검증 실패 시 CP는 `503` 또는 `403`으로 적용 거부.

### 정책·감사·승인 경계
- `POST`/`publish`/`rollback` 은 `require_l5` (infra-admin)만 허용. L4는 `GET` 읽기만.
- 감사: `security.app.audit_ledger` 가 있으면 거기에, 없으면 모듈 내 `_audit_events` 리스트에 `tenant, version, action, actor, timestamp, signature` 기록. 적용 전 경계에서 조회 가능.
- 승인: Stage-1은 별도 승인 레코드 없이 L5 gate로 동작. Stage-2에서 `approval_requests` 연동 예정 (본 문서에 인터페이스 자리 표시).

### 엔드포인트
- Admin API (`admin-console/backend/runtime_config.py`, prefix `/v1/runtime/config`):
  - `GET  /` — 현재 published snapshot (없으면 404, prod에서는 fail-closed 503 아님 — 미게시 상태는 정상)
  - `POST /snapshot` — L5, body `{tenant_id?, expected_version?, note?}` → canonical 수집 + 서명 + 저장, 201 + `version`
  - `POST /publish` — L5, body `{tenant_id?, version}` → published 포인터 이동, 200
  - `GET  /snapshots` — history list (tenant scope)
  - `GET  /snapshots/{version}` — 특정 버전 조회 (서명 포함)
  - `POST /rollback` — L5, body `{tenant_id?, version}` → published를 과거 버전으로 이동
  - `GET  /status` — published 버전, 적용 상태 자리표시자 (CP가 실제 적용 상태 반환)
  - `GET  /audit` — 최근 audit events (L5)
- Control Plane (`control_plane/runtime_config.py`, prefix `/v1/runtime-config`):
  - `GET  /` — DB/HTTP로 Admin published snapshot 조회 + 서명 검증 + 미검증 시 502
  - `GET  /status` — `{published_version, verified, applied_version, applied_at, applied_by, process_identity, signature_valid}`
  - `POST /apply` — 검증된 published snapshot을 `applied`로 마킹 (CP 내부, L5 또는 service token)

### Secret 취급
- Snapshot에 `encrypted_api_key`, `apiKey`, plain secret 절대 포함 금지 — `secret_ref`/`vault_backend`/`provider/model`만.
- 테스트에서 raw secret 유입 시 마스킹 검증.

## 3. 실제 운영 반영 (부모 게이트)
- 본 Stage-1은 코드·테스트·read-only 검증만 수행. DB 마이그레이션/alembic upgrade/서비스 재기동/Mattermost push/git push는 부모의 별도 승인·백업·재기동 게이트에서 진행.
- 운영 반영 전 백업: `pg_dump oaos` + `alembic current` 확인.

## 4. 참조 파일
- Admin: `admin-console/backend/runtime_config.py`
- CP: `control-plane/control_plane/runtime_config.py`
- Tests: `tests/test_runtime_config_plane.py`
- DDL draft §5

## 5. DDL 초안 (미실행, Stage-2 승격 시 Alembic 015)

```sql
-- admin_runtime_config_snapshots: durable versioned snapshots
CREATE TABLE IF NOT EXISTS admin_runtime_config_snapshots (
  id TEXT PRIMARY KEY,                    -- tenant:version or uuid
  tenant_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',   -- draft|published|rolled_back
  snapshot_json TEXT NOT NULL,            -- canonical JSON (config+metadata)
  signature TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  published_by TEXT,
  published_at TIMESTAMPTZ,
  parent_version INTEGER,
  rollback_from INTEGER,
  extra JSONB
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_runtime_config_tenant_version
  ON admin_runtime_config_snapshots (tenant_id, version);
CREATE INDEX IF NOT EXISTS ix_runtime_config_tenant_status
  ON admin_runtime_config_snapshots (tenant_id, status);

-- admin_runtime_config_published: per-tenant published pointer (or reuse admin_settings)
CREATE TABLE IF NOT EXISTS admin_runtime_config_published (
  tenant_id TEXT PRIMARY KEY,
  published_version INTEGER NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  updated_by TEXT NOT NULL
);
```

Alembic `015_runtime_config_snapshots.py` 는 위 DDL을 `op.create_table` + idempotent `IF NOT EXISTS` 검사로 작성 예정.

## 6. 검증 결과 (Stage-1)
- `pytest tests/test_runtime_config_plane.py -q` — 18 passed
- `pytest tests/test_admin_backend.py tests/test_control_plane_api.py -q` — 기존 회귀 유지
- `pytest tests/test_p0_idempotency.py tests/test_p1_live_knowledge_index_operational.py -q` — P0/P1 비침투 확인
