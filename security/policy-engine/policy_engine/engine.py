"""Policy Engine — Section 25. Deterministic, never LLM-based.
- fnmatch 기반 resource 매칭
- Section 25 순서 Strict (POLICY_EVALUATION_ORDER)
- Explicit Deny가 Personal Delegation override
"""
from __future__ import annotations

import fnmatch

from policy_model import (
    PolicyBundle,
    PolicyDecision,
    PolicyEvaluationRequest,
    PolicyEvaluationResult,
    PolicyRule,
    PolicySource,
    POLICY_EVALUATION_ORDER,
)


class PolicyEngine:
    """Section 25 — Deterministic evaluator.

    평가 순서(POLICY_EVALUATION_ORDER)가 절대 우선순위를 결정한다.
    동일 source 내에서는 priority 낮은 값 → id 사전순으로 정렬.
    resource_pattern 은 fnmatch glob 으로 매칭한다.
    """

    def __init__(self, bundles: list[PolicyBundle]) -> None:
        self.bundles = bundles

    def evaluate(self, req: PolicyEvaluationRequest) -> PolicyEvaluationResult:
        # 모든 룰 수집
        all_rules: list[PolicyRule] = []
        for b in self.bundles:
            all_rules.extend(b.rules)

        # source 별 그룹화
        by_source: dict[PolicySource, list[PolicyRule]] = {
            s: [] for s in POLICY_EVALUATION_ORDER
        }
        for r in all_rules:
            # 알 수 없는 source 는 무시 (방어)
            if r.source in by_source:
                by_source[r.source].append(r)
            else:
                # 그래도 EVALUATION_ORDER 에 없으면 DEFAULT_DENY 이전에 넣지 않음
                pass

        # Section 25 순서대로 엄격 평가
        for source in POLICY_EVALUATION_ORDER:
            # DEFAULT_DENY 는 매칭 룰이 없으면 기본 deny 로 처리
            if source == PolicySource.DEFAULT_DENY:
                continue

            rules = sorted(by_source[source], key=lambda x: (x.priority, x.id))
            for rule in rules:
                # action 매칭: "*" 는 와일드카드
                if rule.action != "*" and rule.action != req.action:
                    continue
                # resource 매칭: fnmatch 기반 glob
                # canonical resource 는 slash 구분 문자열
                if not fnmatch.fnmatch(req.resource, rule.resource_pattern):
                    continue

                # 매칭된 첫 번째 룰이 해당 source 의 결정
                # Explicit Deny 가 먼저 평가되므로 Personal Delegation 을 override 함 (Section 25 주의사항)
                return PolicyEvaluationResult(
                    decision=rule.effect,
                    matched_rule=rule,
                    source=source,
                    reason=f"matched {rule.id} @ {source.value}",
                    policy_version=rule.id,
                )

        # 어느 source 에도 매칭되지 않으면 default deny
        return PolicyEvaluationResult(
            decision=PolicyDecision.DENY,
            matched_rule=None,
            source=PolicySource.DEFAULT_DENY,
            reason="no matching allow rule — default deny",
        )

    def evaluate_with_trace(
        self, req: PolicyEvaluationRequest
    ) -> tuple[PolicyEvaluationResult, list[str]]:
        """디버깅용 trace 포함 평가."""
        trace: list[str] = []
        result = self.evaluate(req)
        trace.append(f"decision={result.decision} source={result.source.value} reason={result.reason}")
        return result, trace
