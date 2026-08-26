"""Policy Engine — Section 25. Deterministic, never LLM-based."""
from policy_model import PolicySource, PolicyDecision, PolicyBundle, PolicyEvaluationRequest, PolicyEvaluationResult, PolicyRule, POLICY_EVALUATION_ORDER
import fnmatch

class PolicyEngine:
    def __init__(self, bundles: list[PolicyBundle]):
        self.bundles = bundles

    def evaluate(self, req: PolicyEvaluationRequest) -> PolicyEvaluationResult:
        # Collect all rules, sorted by POLICY_EVALUATION_ORDER priority
        all_rules: list[PolicyRule] = []
        for b in self.bundles:
            all_rules.extend(b.rules)
        # group by source priority
        by_source: dict[PolicySource, list[PolicyRule]] = {s: [] for s in POLICY_EVALUATION_ORDER}
        for r in all_rules:
            by_source[r.source].append(r)

        for source in POLICY_EVALUATION_ORDER:
            for rule in by_source[source]:
                if rule.action != req.action and rule.action != "*":
                    continue
                if not fnmatch.fnmatch(req.resource, rule.resource_pattern):
                    continue
                # Personal Delegation override check: Explicit Deny always wins (Section 25)
                return PolicyEvaluationResult(
                    decision=rule.effect,
                    matched_rule=rule,
                    source=source,
                    reason=f"matched {rule.id} @ {source.value}",
                    policy_version=rule.id,
                )
        # default deny
        return PolicyEvaluationResult(
            decision=PolicyDecision.DENY,
            matched_rule=None,
            source=PolicySource.DEFAULT_DENY,
            reason="no matching allow rule",
        )
