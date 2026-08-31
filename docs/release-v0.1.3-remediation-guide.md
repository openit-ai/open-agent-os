# OAOS v0.1.3 점검·수정·릴리즈 가이드

> 작성일: 2026-08-31 (KST)
> 기준 저장소: `openit-ai/open-agent-os`
> 기준 아키텍처: `docs/architecture-v1.7.2.md` 및 `docs/architecture-v1.7.2-design.md`
> 목표 제품 버전: `v0.1.3`
> 운영 원칙: **백업 → 승인 → 변경 → 테스트 → 서비스/API/UI read-back → 커밋 → 릴리즈 승인**

## 1. 기준선과 범위

### 1.1 버전 구분

- 제품 버전: `0.1.3`
- 아키텍처 문서 버전: `v1.7.2`
- 현재 원격 기준: `origin/main` (`d1d92f857f0f8d5718356be689c9360f2e2137d0`)
- 기존 제품 태그: `v0.1.2` (`34f0981e71ead3fcfb07b4594542992d8cfbee83`)
- v0.1.3은 v1.7.2 아키텍처의 운영·검증 잔여를 해소하는 제품 릴리즈로 관리한다.

### 1.2 작업 경계

- 이 문서는 지속 참조용 실행 기준이다.
- 구현 파일을 수정하기 전 현재 작업 트리와 기존 미커밋·백업 파일을 보존한다.
- 기존 작업자의 미커밋 변경을 임의로 삭제·reset·stash·재작성하지 않는다.
- 기능 변경과 문서 변경은 가능한 한 별도 커밋으로 분리한다.
- `main` push, GitHub Release 생성, 운영 서비스 재기동·배포는 별도 승인 없이는 수행하지 않는다.
- 시스템·서비스 재기동은 실행 직전에 마스터 확인 게이트를 거친다.
- 실제 외부 자격증명·과금 API·운영 DB는 승인된 staging 범위에서만 사용한다.

## 2. 완료 판정 기준

### 2.1 증거 계층

각 항목은 아래 네 계층을 구분해 기록한다.

1. **Repository**: 코드·테스트·정적 설정·문서
2. **Process**: 실제 실행 프로세스가 최신 커밋과 환경파일을 로드했는지
3. **Runtime**: 실제 ingress부터 외부 응답까지 왕복한 결과
4. **Distributed/External**: 실제 Redis·다중 replica·Kubernetes/CNI·외부 connector 결과

단위 테스트만으로 Process/Runtime/Distributed/External 완료를 주장하지 않는다.

### 2.2 릴리즈 차단 조건

다음 중 하나라도 남으면 v0.1.3 릴리즈 후보를 PASS로 판정하지 않는다.

- 전체 테스트 실패, timeout, 미수집 테스트 또는 원인 미분석 실패
- P0 보안·데이터 보호 결함
- 실제 실행 경로에서 인증·tenant·agent 격리 실패
- production에서 mock/fallback/no-op 우회 가능
- Vault 외부화 정책과 실제 secret 저장 방식이 불일치한 채 문서가 완료를 주장
- Profile/RAG 핵심 경로가 구현됐다고만 기록되고 실제 동작 증거가 없음
- 증거 문서의 commit·timestamp·test count가 현재 릴리즈 후보와 불일치

## 3. 우선순위별 실행 순서

## P0 — 릴리즈 차단

### P0-1. Vault secret 저장 경계 확정

**설계 기준**

- DB에는 `secret_ref`와 메타데이터만 보관
- 실제 secret material은 승인된 외부 backend 또는 명시적으로 승인된 대체 경로에 보관
- `EncryptedPostgresVault` legacy 사용 여부를 문서와 runtime에서 동일하게 표시

**확인 대상**

- `security/app.py`
- `security/credential-vault/vault/vault.py`
- `security/credential-vault/vault/external.py`
- `security/models/orm.py`
- `alembic/versions/005_vault_secret_ref.py`
- `docs/vault-externalization-design.md`

**TDD/검증**

1. DB에 secret bytes가 저장되지 않는 실패 테스트 작성
2. 외부 backend 조회·실패·회수(revoke) 테스트 작성
3. legacy backend를 명시적으로 차단하거나 migration-only로 제한
4. migration 및 rollback을 staging DB에서 검증
5. API·Admin Console·provider 호출에서 secret 값 미노출 read-back

**완료 증거**

- schema read-back
- migration 전후 row 비교
- secret 값이 로그·audit·응답·Git에 나타나지 않는 grep/구조 검증
- 기존 provider/vault 회귀 테스트 PASS

### P0-2. 전체 테스트와 권한 경계 실패 해소

- `pytest -q`를 단일 프로세스로 실행한다.
- 실패·timeout을 성공으로 기록하지 않는다.
- IAM/policy/approval/auth 관련 실패는 P0로 승격해 원인을 먼저 해결한다.
- 테스트 수는 실행 결과에서 자동 추출해 문서에 반영한다.

**필수 명령**

```bash
python -m pytest -q
python scripts/verify-evidence-tiers.py --check-only
```

## P1 — 운영 기능·통합

### P1-1. Live Knowledge Index/RAG

- staging Outline/Notion credential과 네트워크를 사용한다.
- `scripts/verify-knowledge-live.py --health` 실행
- 제한된 corpus로 다음을 순서대로 검증한다.
  1. 최초 동기화
  2. 동일 `content_hash` 재실행 no-op
  3. 문서 수정 재임베딩
  4. ACL 변경 invalidation/revalidation
  5. 삭제 반영
  6. checkpoint와 bounded retry
  7. source URL·provenance·수정시각 read-back
- 전체 corpus backfill은 별도 실행 로그와 중단·재개 지표를 남긴다.
- 외부 API 실패 시 fake/mock 결과를 live evidence로 승격하지 않는다.

### P1-2. Adaptive Profile 전체 경로

- `alembic/versions/014_adaptive_profile.py`
- `control-plane/control_plane/adaptive_profile/`
- `control-plane/control_plane/acp_adapter.py`
- `control-plane/control_plane/mattermost_adapter/webhook.py`

다음 경로를 실제로 검증한다.

```text
Mattermost ingress
→ identity/session
→ evidence worker
→ profile persistence
→ cache invalidation
→ minimal response policy
→ Hermes/LLM critical path
→ response
→ audit/read-back
```

필수 항목:

- 현재 직접 지시가 저장 선호보다 우선
- cross-tenant/cross-user 차단
- Evidence idempotency
- worker 재시도·중복 이벤트 안전성
- cache stale 방지 및 invalidation
- Profile Skill이 실제 runtime registry에 등록됨
- hook 장애 시 기본 정책으로 안전하게 축소
- UI가 없으면 미구현으로 기록

### P1-3. Distributed state와 NetworkPolicy

실제 staging에서 다음을 검증한다.

- Redis 실서버
- Control Plane 2 replica
- quota/rate/replay/session 동시성
- Redis 장애 시 production 503
- `k6` 병렬 요청
- kind/Kubernetes
- CNI enforcement
- Hubble `DROPPED` flow evidence

`fakeredis`, static YAML 검사, 단일 프로세스 테스트는 Repository 증거로만 기록한다.

### P1-4. 실제 외부 왕복

최소 1건의 검증 가능한 시나리오를 남긴다.

```text
Mattermost → OAOS → Hermes/LLM → Execution Gateway
→ Policy/Approval → Connector → Audit/Personal Wiki → Mattermost thread
```

응답 post/thread ID, trace ID, audit event ID, source reference를 read-back한다.

## P2 — 품질·정합성·부채

### P2-1. 버전과 문서 정합성

- `admin-console/package.json`
- root `pyproject.toml`
- 각 `packages/*/pyproject.toml`
- `README.md`, `README.ko.md`, `SECURITY.md`
- `docs/architecture-v1.7.2.md`
- `docs/evidence-report-v1.7.1.json`
- `docs/deployment-verification-v0.1.3.md`

제품 버전 `0.1.3`, 아키텍처 `v1.7.2`, 검증 대상 commit을 혼동하지 않는다.
문서의 테스트 수·timestamp·commit은 v0.1.3 후보에서 재생성한다.

### P2-2. 저장소 위생

- `.bak`, `.backup_*`, `.pytest-cache`, `.e2e-backups`는 먼저 목록·용도를 보존 기록한다.
- 작업자 변경분과 생성 산출물을 분리한다.
- 삭제는 별도 cleanup 커밋에서만 수행하며, 복원 필요 여부 확인 후 진행한다.
- 소스·secret·운영 데이터가 backup 디렉터리에 포함되지 않았는지 검사한다.

### P2-3. 단일 source of truth

- 중복된 `knowledge_index/`와 `packages/knowledge-index/`의 runtime import 경로를 확정한다.
- 중복 구현을 제거하기 전 import graph와 packaging/build 결과를 검증한다.
- env gate, version, runtime config, policy loader도 정본과 mirror를 구분한다.

### P2-4. 테스트 실행성

- unit/integration/distributed/external 테스트를 명확히 분리한다.
- 각 그룹 timeout과 prerequisite를 기록한다.
- timeout·skip·unavailable은 PASS로 세지 않는다.
- 전체 suite가 5분 이상이면 원인별 병렬 분리와 CI timeout을 설계하되, 전체 게이트 자체를 생략하지 않는다.

## 4. 단계별 커밋·검증 규칙

1. **Baseline**: `git status`, HEAD, origin, tag, 프로세스·테스트 상태 저장
2. **Guide**: 이 문서 커밋
3. **P0 기능**: 실패 테스트 → 최소 수정 → 집중 테스트 → 관련 전체 테스트
4. **P0 문서**: 구현과 분리해 증거 문서 갱신
5. **P1 기능**: RAG/Profile/distributed/external을 서로 다른 변경 단위로 처리
6. **P2 정리**: 버전·문서·backup·패키징 정리
7. **Release candidate**: clean checkout에서 full test와 evidence 재생성
8. **릴리즈 승인**: 태그·push·GitHub Release는 마스터 최종 승인 후 수행

권장 커밋 예시:

```text
fix(security): externalize vault secret material
fix(rag): verify live knowledge index synchronization
fix(profile): complete runtime policy injection path
test(distributed): verify redis two-replica invariants
chore(release): align v0.1.3 version metadata
docs(release): record v0.1.3 verification evidence
```

## 5. 현재 기준선에서 확인된 주의사항

- 현재 작업 트리가 detached HEAD이며, 기존 미추적 backup·작업 산출물이 다수 존재할 수 있다.
- 이러한 파일은 삭제·reset하지 않고 먼저 inventory한다.
- `origin/main`과 로컬 작업 트리가 다를 수 있으므로, 이후 모든 보고서에 `HEAD`, `origin/main`, `tag`, `dirty/untracked`를 함께 기록한다.
- 이전 evidence의 `unit:927`, `distributed:0`, `external:0`은 최신 v0.1.3 검증 결과로 재사용하지 않는다.
- 집중 테스트 통과는 전체 릴리즈 PASS가 아니다.

## 6. 최종 릴리즈 체크리스트

- [ ] P0 Vault 저장 경계 및 migration 검증
- [ ] P0 전체 pytest green, timeout/failed 0
- [ ] P1 Live RAG health/backfill/ACL/deletion read-back
- [ ] P1 Profile worker/cache/Skill/Hermes runtime E2E
- [ ] P1 Redis 2-replica/k6 및 CNI/Hubble 증거
- [ ] P1 Mattermost→OAOS→runtime→thread 실제 왕복
- [ ] P2 버전 metadata `0.1.3` 정합성
- [ ] P2 architecture/evidence 문서 commit·timestamp 갱신
- [ ] P2 backup/untracked 파일 inventory 및 cleanup 분리
- [ ] clean checkout 재검증
- [ ] release candidate 파일·로그·URL을 마스터에게 선검토용 전달
- [ ] 마스터 승인 후에만 push/tag/GitHub Release/배포

## 7. 보고 형식

각 단계 완료 시 아래 형식으로 보고한다.

```text
단계: P0/P1/P2 - 항목
상태: PASS | PARTIAL | FAIL | BLOCKED
기준 커밋:
변경 파일:
실행 명령:
테스트 결과:
Runtime/External 증거:
잔여 위험:
다음 단계:
```
