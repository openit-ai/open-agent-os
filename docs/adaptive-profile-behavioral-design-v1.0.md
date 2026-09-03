# Adaptive Profile 행동 기반 성향 분석 설계 v1.0

> 상태: 설계안 — 구현·운영 적용 전
> 기준: OAOS 아키텍처 v1.7.2 §16.12
> 작성일: 2026-08-31 (KST)

## 1. 목적과 범위

현재 Adaptive Profile MVP는 정해진 표현을 정규식으로 추출하는 수준이다. 목표 구현은 대화 스타일, 사용 빈도, 업무 진행 방식을 여러 상호작용에서 관찰해 **행동 특성 벡터**를 축적하고, 그 결과를 개인화 응답 정책에 반영하는 것이다.

MBTI·애니어그램은 사용자를 진단하거나 고정 분류하는 값이 아니다. 충분한 근거가 축적된 경우에만 행동 벡터를 사람이 이해하기 쉬운 **유사 축/참고 레이블**로 표시한다. 저장·런타임 권한·업무 자동화의 근거로 직접 사용하지 않는다.

금지:

- 심리·의학·정신건강 진단
- 인사평가·채용·승진·급여·순위화
- 근거 없는 MBTI/애니어그램 단정
- 원문 전체의 장기 Profile 복제
- Profile을 이용한 승인·권한·결제·삭제 우회

## 2. 계층 구조

```text
원문 상호작용
  → 이벤트 정규화·PII 최소화
  → 관찰 특징(feature)
  → 증거(evidence: source, confidence, provenance)
  → 행동 특성 벡터(trait scores)
  → 시간 감쇠·충돌 조정·신뢰도
  → task별 Response Policy
  → 선택적 유형 참고 표시(MBTI-like / Enneagram-like)
```

### 2.1 세 데이터의 분리

- Personal Wiki: 대화·업무 사실의 검색 가능한 원문/요약 저장소
- Behavioral Profile: `trait + score + confidence + evidence_count + last_observed`만 저장
- Type Projection: 벡터에서 계산되는 비권위적 표시값. 원천 데이터가 아니며 낮은 confidence에서는 `insufficient_evidence` 반환

## 3. 행동 특성 벡터

초기 축은 다음 8개 상위 차원과 23개 기존 trait로 구성한다.

- 응답 선호: `conclusion_first`, `verbosity`, `directness`, `explanation_depth`, `repetition_tolerance`
- 근거·판단: `evidence_requirement`, `quantitative_preference`, `critical_challenge`, `uncertainty_tolerance`, `recommendation_decisiveness`, `alternative_preference`
- 업무 실행: `risk_tolerance`, `decision_speed`, `agent_autonomy`, `confirmation_requirement`, `planning_orientation`, `completion_orientation`
- 협업·변화: `delegation_preference`, `control_preference`, `disagreement_tolerance`, `experimentation_preference`, `novelty_preference`

각 trait는 다음 구조다.

```json
{
  "score": -1.0,
  "confidence": 0.0,
  "sample_count": 0,
  "effective_sample_count": 0.0,
  "last_observed_at": null,
  "task_scores": {}
}
```

`score`는 선호 방향, `confidence`는 관찰량·일관성·시간 신선도의 결합값이다. 단일 문장으로 높은 confidence를 만들지 않는다.

## 4. 관찰 소스와 추출

### 4.1 증거 유형

| source_type | 예 | 기본 가중치 |
|---|---|---:|
| explicit_feedback | “결론부터”, “간결하게” | 1.00 |
| repeated_correction | 같은 방향의 답변 수정 반복 | 0.90 |
| actual_choice | 대안 중 선택·실행한 항목 | 0.85 |
| work_pattern | 위임·검토·완료·재작업 흐름 | 0.70 |
| general_expression | 반복되는 자연어 표현·구조 | 0.40 |
| style_inference | 문장·응답 상호작용 통계 | 0.25 |

### 4.2 정규화 이벤트

모든 이벤트는 검증된 `tenant_id`, `user_id`, `agent_id`, `session_id`, `conversation_id`, `message_id`, `task_type`, `observed_at`, `source_ref`를 가진다. 원문은 Profile 테이블에 저장하지 않고 해시·짧은 matched feature·provenance만 보관한다.

관찰 feature 예:

- 대화 빈도: 일/주간 활성일, 세션 간격, 업무 시간대 분포
- 대화 스타일: 평균 발화 길이, 질문/지시 비율, 재질문·수정 비율, 결론·근거·대안 요구 비율
- 업무 진행: 계획→실행→검증 순서, 위임 비율, 승인 요청/거절 비율, 완료 후속률, 재작업률
- 선택 행동: 여러 대안 중 선택, 초안 수정, 도구 실행 승인/취소

빈도는 활동량 자체를 성향으로 취급하지 않는다. 예를 들어 사용량이 많다는 사실만으로 외향성·성실성을 추론하지 않으며, 업무 종류와 시간대 편향을 함께 기록한다.

### 4.3 추출 파이프라인

1. deterministic parser가 명시 표현·행동 이벤트를 추출한다.
2. feature aggregator가 개인/작업 유형별 집계값을 갱신한다.
3. 규칙 기반 evidence가 우선 저장된다.
4. 선택적 분석기는 비동기·저비용·허용된 모델로 구조화 feature만 반환한다. 원문을 Profile에 복제하지 않는다.
5. 최소 관찰 수와 일관성 검사를 통과한 evidence만 score에 반영한다.
6. 실패 시 기존 score를 변경하지 않고 재처리 대기·감사 로그를 남긴다.

### 4.4 응답 경로 성능 불변식

성향 분석은 대화 응답의 critical path에 들어가지 않는다.

- Mattermost ingress 응답 경로는 identity·idempotency·session append·ACP 전달만 수행한다.
- feature 추출·집계·Profile DB write·projection 생성·cache invalidation은 durable event를 기록한 뒤 `asyncio.create_task` 또는 bounded worker queue로 비동기 처리한다.
- 파일 journal append, 동기 DB/Redis 호출, LLM 호출, 대량 history scan은 request handler에서 직접 실행하지 않는다. 필요한 동기 함수는 `asyncio.to_thread`로 bounded executor에 격리한다.
- 사용자 응답을 기다리는 timeout과 분석 작업 timeout을 분리한다. 분석 실패·지연은 현재 답변을 실패시키지 않으며 retry/dead-letter 상태로 남긴다.
- hard budget: ingress 추가 CPU ≤ 5 ms 목표, enqueue ≤ 20 ms 목표, 응답 critical path에서 Profile 분석 대기 0 ms. queue backlog·worker latency·drop/retry count를 메트릭으로 기록한다.
- bounded concurrency와 backpressure를 사용한다. 큐가 가득 차면 최신 분석을 무한히 쌓지 않고 집계 가능한 이벤트로 coalesce하며, 원문 재전송은 하지 않는다.

현재 운영 코드의 `control_plane/control_plane/mattermost_adapter/webhook.py`에는 `_archive_conversation_turn()`을 request handler에서 동기 호출하는 구현이 있으므로, 이 설계의 성능 불변식을 아직 만족하지 않는다. 구현 단계에서 journal append를 비동기 큐로 이동하고, 느린 archive/worker 중에도 독립 health와 일반 응답 latency를 검증하는 회귀 테스트를 먼저 추가한다.

## 5. 시간·충돌·신뢰도

- 새 관찰은 EMA로 반영하되 source weight와 confidence를 곱한다.
- 반대 방향 관찰은 삭제하지 않고 동일 trait의 충돌 evidence로 보존한다.
- 최근 관찰에 시간 감쇠를 적용한다. 장기 미사용은 confidence를 낮추며 score를 임의로 확정하지 않는다.
- 동일 `tenant + user + source_ref + feature + observed_at_bucket`는 idempotency hash로 중복 차단한다.
- 권장 최소 기준: 단일 source 1건은 `confidence <= 0.45`; 서로 다른 세션 3건 이상이고 방향 일관성이 있을 때만 정책 반영 후보; 유형 표시에는 더 높은 기준 적용.

신뢰도는 다음 요소의 결합이다.

```text
confidence = sample_strength × cross_session_consistency × freshness × source_reliability
```

## 6. MBTI/애니어그램 유사 투영

### 6.1 원칙

MBTI와 애니어그램을 심리검사 결과처럼 산출하지 않는다. 기존 행동 벡터의 여러 축을 설명용 projection으로 매핑한다. projection은 버전이 있는 순수 함수이며 원본 score를 변경하지 않는다.

### 6.2 MBTI-like 축 예시

- E/I-like: 대화량이 아니라 협업·외부 상호작용 선택 경향 — 현재는 기본 `insufficient_evidence`
- S/N-like: 구체 사실/절차 선호 ↔ 가능성/아이디어 선호
- T/F-like: 기준·수치·논리 선호 ↔ 관계·맥락·가치 고려
- J/P-like: 계획·완료·결정 구조 선호 ↔ 탐색·실험·변경 수용

### 6.3 Enneagram-like 표시

유형 번호를 성격 진단으로 제시하지 않고, 동기 가설을 표시한다. 예: `control_and_certainty_like`, `achievement_and_completion_like`, `novelty_and_exploration_like`. 각 가설에는 기여 trait, score, confidence, 근거 수, 관찰 기간을 함께 표시한다.

결과 예:

```json
{
  "projection_version": "1.0",
  "status": "insufficient_evidence",
  "mbti_like": {"axes": {"J_P": {"label": "J-like", "confidence": 0.71}}},
  "enneagram_like": [],
  "disclaimer": "행동 관찰 기반 참고 표시이며 성격·심리 진단이 아닙니다."
}
```

`confidence < 0.70` 또는 필요한 축의 sample threshold 미달이면 label 대신 `uncertain`을 반환한다.

## 7. 저장 모델 확장

기존 `user_profiles`, `trait_scores`, `task_trait_scores`, `profile_evidence`, `explicit_preferences`를 유지하고 다음을 추가한다.

- `profile_observations`: feature_name, normalized_value, source_type, source_ref_hash, observed_at, session_id, task_type
- `profile_feature_aggregates`: feature_name, window, count, mean, variance, last_observed_at
- `profile_projections`: projection_version, result_json, confidence, generated_at, input_profile_version
- `profile_settings`: learning_enabled, retention_days, projection_enabled, consent_updated_at

모든 테이블의 PK/unique/index는 `tenant_id + user_id`를 포함한다. 원문·민감 원인은 저장하지 않고 필요 시 Personal Wiki의 권한 있는 source_ref로만 연결한다.

## 8. Response Policy 연결

현재 지시 > 명시 preference > task trait > global trait > 기본 정책 순서를 유지한다. Profile은 다음 최소 정책만 생성한다.

```json
{
  "conclusion_first": true,
  "verbosity": "medium",
  "technical_depth": "high",
  "evidence_requirement": "high",
  "challenge_assumptions": true,
  "alternatives": 2,
  "confirmation_level": "low"
}
```

LLM에는 score·evidence·유형·분석 원문을 보내지 않는다. Profile Hook 장애 시 기본 정책으로 축소한다. 성향 결과는 tool 권한·정책·승인에 영향을 주지 않는다.

## 9. API·Skill

본인 범위에서만 제공한다.

- `GET /v1/profile/me`: score가 아닌 trait 수준·confidence·관찰 기간
- `GET /v1/profile/policy`: 현재 task의 최소 Response Policy
- `GET /v1/profile/projection`: MBTI-like/Enneagram-like 참고 표시와 disclaimer
- `POST /v1/profile/settings`: 학습 중단·보존기간·projection 사용 설정
- `POST /v1/profile/reset`: evidence·aggregate·projection 초기화
- Skill: `explain_my_profile`는 추정과 사실을 구분하고 근거 수·신뢰도·기간을 표시

## 10. 개인정보·안전

- 기본 학습은 사용자 opt-out 가능, 중단 즉시 새로운 observation을 반영하지 않는다.
- 보존기간 만료 시 observation과 projection을 삭제하고 aggregate는 재계산한다.
- cross-tenant/user 조회 차단, 모든 변경 audit, export/delete 지원.
- Profile은 조직 정책보다 항상 낮은 우선순위다.
- 관리자 화면은 개인 상세 기본 비노출·비식별 집계만 허용한다.

## 11. 구현 순서와 검증

1. **Phase A — 계약·저장:** observation/aggregate/projection schema와 migration, retention/settings API
2. **Phase B — 관찰:** deterministic feature extractor, session/activity aggregator, repeated correction/choice/work-pattern evidence
3. **Phase C — 학습:** confidence·decay·contradiction·idempotency와 기존 worker 통합
4. **Phase D — 투영:** versioned MBTI-like/Enneagram-like 순수 함수와 `insufficient_evidence` gate
5. **Phase E — 런타임:** Profile API/Skill/ACP Hook에서 최소 policy만 주입
6. **Phase F — 운영:** read-only staging backfill, DB row/schema/cache read-back, 사용자 승인 후 production 적용

필수 테스트:

- 표현 변형·오탐·다국어 deterministic extraction
- 서로 다른 세션의 feature aggregation과 시간창 계산
- 반복 수정·실제 선택·업무 패턴 source weight
- 반대 증거·감쇠·confidence threshold
- tenant/user 격리·opt-out·reset·retention
- projection version, insufficient evidence, disclaimer
- current instruction 우선순위와 score/evidence 비노출
- worker idempotency·재시도·DB 장애 fail-closed
- 실제 Mattermost 한 건으로 observation→DB→policy cache→응답 read-back

## 12. 완료 기준

- Code: 각 Phase의 코드·migration·테스트 존재
- Persistence: 실제 운영 DB schema/head/row/index read-back
- Runtime: 소유 서비스 재기동 후 OpenAPI·env·PID·readiness 확인
- User-path: 새 Mattermost 메시지에서 exact post/session/evidence/profile_version/response read-back
- Distributed/External: 별도 증거 없이는 PASS하지 않음
- 결과 표기: `measured_behavior_profile`과 `type_projection`을 UI/API에서 분리
