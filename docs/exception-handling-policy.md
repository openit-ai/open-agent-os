# OAOS 공통 예외처리 정책 v12.1

- 단계: 안정화 12.1 (정책 확정, 문서 중심)
- 성격: production lint 정리(`BLE001` / `S110` / `S112`)에 앞선 기준 고정 문서
- 원칙: 본 문서는 정책/문서 산출물이며, 애플리케이션 코드 수정을 포함하지 않는다.

## 0. 진척 지표

- 진척 지표는 raw lint count가 아니라 **unique except block 수**를 사용한다.
- 동일 `try/except` 블록에서 `BLE001+S110` / `BLE001+S112`가 중복 검출되므로 raw count는 규모를 과대평가한다.

현재 baseline (12.0 분석 기준):

| 구분 | unique except block |
|---|---|
| production | 1,865 |
| tests | 225 |
| 전체 | 2,090 |

- 12.2 이후 진척은 위 unique 수를 기준으로 측정한다.
- raw count(`BLE001` 약 2,018 / `S110` 약 935 / `S112` 약 57)는 참고용으로만 사용한다.

## 1. production 기본 원칙

### 1.1 Security / Auth / IAM / Approval / Delegation / Vault / Audit

- production **fail-open 금지**.
- 인증/인가/정책/감사 실패는 **fail-closed 기본**.
- **silent `pass` / `continue` 금지**.
- 예외 무시가 필요한 cleanup(`rollback` / `close` / `dispose` 등)에 한해 별도 허용하며, 이 경우에도 2장 Cleanup 규칙을 따른다.
- 해당 영역의 예외처리는 원칙적으로 **리뷰 필수**로 분류한다.

### 1.2 Cleanup

- `rollback` / `close` / `dispose` / `unlink` 등은 best-effort cleanup으로 인정할 수 있다.
- `except Exception` 대신 가능한 구체 예외를 사용한다.
  - 예: DB 정리(`SQLAlchemyError`), 파일 정리(`OSError`), 소켓/연결 정리(해당 라이브러리 예외).
- cleanup 실패가 본 처리 결과를 바꾸지 않는 경우에 한해 `logger.debug`를 허용한다.
- cleanup 실패가 데이터 정합성에 영향을 줄 수 있으면 `warning` 이상으로 기록한다.
- 단순 `pass`만 있는 cleanup은 production에서 원칙적으로 허용하지 않는다.

### 1.3 Optional dependency / availability probe

- 선택 의존성 및 존재 여부 확인은 `ImportError` / `ModuleNotFoundError` 등으로 좁힌다.
- `except Exception` 금지.
- probe 실패와 런타임 오류를 구분한다.
  - 허용: 모듈 없음 → 기능 비활성화 / `None` 반환 / 대체 경로 선택.
  - 금지: import 이후 실제 호출 실패까지 같은 `except`로 삼키기.

### 1.4 Retry / recovery

- retry 횟수와 backoff를 명시한다.
- 마지막 실패는 로그 또는 명시적 예외 전달 없이 버리지 않는다.
- 무한 retry 금지.
- production에서 fallback이 보안/정책을 우회하면 금지한다.
  - 예: quota backend 실패 시 non-prod 메모리 fallback은 허용 가능하나, prod 보안 강제 경로의 우회는 금지.

### 1.5 User-facing degradation

- 기능별로 degraded response 허용 여부를 정의한다.
- 허용 시 `warning` + telemetry를 남긴다.
- 핵심 보안/권한/데이터 정합성 기능은 degradation을 금지하고 fail-closed한다.
  - 금지 예: 등록 조회, 권한 판정, 감사 기록, 결제/승인 상태 변경.
  - 허용 검토 예: 부가 아카이브, 컨텍스트 보강, 첨부 미리보기 등 실패해도 주 응답이 성립하는 경로 (단, 허용 목록에 명시된 경우에만).

### 1.6 Logging 후 swallow

- broad `Exception` 허용 여부를 명시적으로 판단한다.
- 허용할 경우 최소 `warning` / `debug` 로그 및 사유가 필요하다.
- 로그는 예외 유형과 컨텍스트(tenant / session / 기능명 중 해당되는 것)를 포함한다.
- 단순 lint 회피용 `noqa`는 금지하며, 6장 suppression 규칙을 따른다.

### 1.7 Silent swallow

- production에서는 원칙 금지이다.
- 특히 security / auth / audit / DB / JWT 영역은 리뷰 필수이다.
- `except Exception: pass` / `except Exception: continue`는 정책 위반 기본값으로 본다.
- 예외적으로 허용하려면 cleanup 또는 명시적 probe에 해당함을 코드에서 확인할 수 있어야 한다.

### 1.8 Fail-open

- production 금지.
- non-production에서만 명시적으로 허용 가능하다.
- 허용 조건: `warning` + fail-open telemetry 필수, prod 진입 시 fail-closed 전환 경로 명시.

## 2. 패턴별 허용/금지/리뷰필수 표

| 패턴 | production | 조건 / 비고 |
|---|---|---|
| A. Best-effort cleanup | 조건부 허용 | 구체 예외 우선. 본 결과 불변 시 `debug` 허용, 정합성 영향 시 `warning` 이상. silent `pass` 단독은 금지 |
| B. Optional capability fallback | 조건부 허용 | 선택 기능에 한함. 보안/권한/정합성 경로의 fallback은 리뷰 필수 |
| C. Availability probe | 조건부 허용 | `ImportError` / `ModuleNotFoundError` 등으로 좁힌 경우만 허용. `except Exception` 금지 |
| D. User-facing degradation | 허용 목록制 | 기능별 허용 여부 정의 필요. 허용 시 `warning` + telemetry. 핵심 보안/권한/정합성은 금지 |
| E. Retry / recovery | 조건부 허용 | 횟수·backoff 명시, 최종 실패 로그/전달, 무한 retry 금지, 보안 우회 fallback 금지 |
| F. Logging 후 swallow | 리뷰 필수 | broad `Exception` 허용 여부 개별 판단. 최소 `warning`/`debug` + 사유 필요. lint 회피용 `noqa` 금지 |
| G. Silent swallow | 원칙 금지 | production 금지. security/auth/audit/DB/JWT는 리뷰 필수 |
| H. Fail-open | production 금지 | non-prod 명시적 허용만 가능. `warning` + telemetry 필수 |
| I. Fail-closed | 허용 | 3장 taxonomy 준수. 원인 예외는 `from e` 등으로 연결 권장 |
| J. Bug-masking 가능 broad catch | 리뷰 필수 | 프로그래밍 오류까지 은폐 가능한 넓은 포착은 구체 예외로 좁힘. 일시 유지 시 사유와 추적 수단 필요 |

## 3. HTTP 오류 taxonomy 고정

| 상태 | 용도 | 예 |
|---|---|---|
| 401 | 인증 토큰/서명/만료 | JWT 무효, 서명 불일치, 토큰 만료 |
| 403 | 권한/정책 거부 | 등록되지 않은 사용자, 에이전트 신원 불일치, 정책 거부 |
| 409 | 상태/버전 충돌 | `VERSION_CONFLICT`, 낙관적 잠금 충돌, 중복 생성 |
| 422 | 입력/검증 오류 | 스키마 검증 실패, 잘못된 입력 형식, 직렬화 불가 입력 |
| 503 | DB/Redis/Vault/외부 backend unavailable | durable DB 쓰기 실패, quota backend 불가, 임베딩 제공자 불가 |

- 위 5종 이외의 상태 코드는 12.2 수정 과정에서 사유와 함께 추가 제안한다.
- 원인 예외가 있는 fail-closed 변환은 `raise HTTPException(...) from e` 형태를 권장한다.
- production에서 backend unavailable을 degraded 응답으로 숨기지 않는다.

## 4. production vs test vs script 정책

### 4.1 production

- 본 문서 1~3장, 6장을 그대로 적용한다.
- 지표: unique 1,865 블록.
- silent 및 fail-open을 최우선 심사 대상으로 한다.

### 4.2 tests

- production보다 완화 가능하다.
- 테스트 teardown/cleanup의 broad exception은 별도 허용을 검토한다.
- 테스트 본문의 `Exception` catch / `raises`는 구체화를 유지한다.
- tests 전체 `BLE001` blanket ignore는 하지 않는다.
- 지표: unique 225 블록. production 지표와 분리 집계한다.

### 4.3 scripts

- 단발성 운영 스크립트 fallback은 별도 등급으로 관리한다.
- silent exception은 최소 stderr/log 출력을 남긴다.
- daemon/bridge 성격 스크립트(`oaos-mm-bridge`류 장기 실행)는 production 정책에 가깝게 적용한다.
  - 폴링/첨부/전송 실패의 `print + return` fallback은 허용 검토 가능.
  - 시작 불가 수준의 초기화 실패는 fail-closed(`SystemExit` 등)를 유지한다.

## 5. BLE001 / S110 / S112 처리 규칙

- `BLE001`: broad `except Exception` / bare `except`를 의미한다.
  - C는 구체 예외로 좁혀 해소한다.
  - A·E는 구체 예외 + 로그로 해소한다.
  - F·I·H는 broad 유지가 필요하면 6장 라인 단위 예외로만 관리한다.
- `S110` (`try-except-pass`): production 원칙 위반이다.
  - cleanup(A) 또는 probe(C)임을 코드로 입증できない 한 유지하지 않는다.
  - secure-path의 `S110`는 리뷰 필수이다.
- `S112` (`try-except-continue`): 반복 루프 내 행사라도 production에서는 원칙 금지이다.
  - 계속 진행이 명세라면 D 허용 목록 + 로그/telemetry 조건을 충족해야 한다.
- 동일 블록 중복(`BLE001+S110`, `BLE001+S112`)은 수정 시 1건으로 처리하고, 진척은 unique 기준으로만 차감한다.

## 6. lint suppression 정책

- blanket `noqa` 금지.
- 광범위 디렉터리 ignore 금지.
- 정말 의도된 broad exception만 정확한 파일/라인 단위 예외를 허용한다.
- suppression에는 사유가 코드에서 확인 가능해야 한다.
  - 허용 예: prod/non-prod 분기가 명시된 fail-open, 허용 목록에 있는 degradation, 횟수·backoff가 명시된 retry.
  - 금지 예: 사유 없는 `noqa: BLE001`, 디렉터리 단위 일괄 면제, 로그 없는 silent에 대한 면제.
- tests/scripts 분리는 blanket ignore가 아니라 production과 분리된 규칙 파일 또는 경로별 명시 규칙으로 관리한다.

## 7. 향후 12.2~12.x 수정 순서

1. **12.2 — 분리 및 기계적 축소 (저위험)**:
   - tests / scripts 규칙 분리로 production 지표 정화.
   - C (availability probe) 구체 예외화.
   - A (cleanup) 구체 예외 + `debug` 표준화.
2. **12.3 — 로깅 표준화 및 면제 확정**:
   - F (logging 후 swallow) 로그 포맷·사유 기준 확정 후 라인 단위 예외 확정.
   - D 허용 목록 초안 확정 (기능별 degraded 허용 여부).
3. **12.4 — 보안·fail-open 심사**:
   - secure-path silent 150곳대 심사 (`auth` / `ledger` / `delegation` / `approval` 우선).
   - H fail-open prod/non-prod 분기 확정 및 telemetry 점검.
4. **12.5 — formative narrowing**:
   - J (bug-masking 가능 broad catch) 도메인 예외로 축소.
   - E retry 헬퍼 공통화 및 무한 retry 제거.
   - I HTTP taxonomy 위반 수정.
5. **12.x — 잔여 unique 소진 및 재발 방지**:
   - unique 2,090 → 0 로드맵의 잔여분 처리.
   - 신규 코드용 예외처리 체크리스트를 본 문서 부록으로 승격.

---

- 본 문서는 12.1 산출물이며 애플리케이션 코드를 변경하지 않는다.
- 이후 단계의 모든 예외처리 수정은 본 문서의 허용/금지/리뷰필수 표를 기준으로 심사한다.
