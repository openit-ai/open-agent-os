"""Adaptive Profile Runtime Hook — minimal Response Policy."""

from __future__ import annotations

import os
import logging
from typing import Any

from .engine import DEFAULT_POLICY, TASK_TYPES, synthesize_policy, validate_task_type

logger = logging.getLogger(__name__)


class AdaptiveProfileHook:
    """Runtime Hook interface.

    before_llm_call: given verified agent context (tenant_id/user_id/task_type),
    returns minimal Response Policy dict (7 keys, no scores/evidence).
    Fail-safe: on any error returns DEFAULT_POLICY.
    after_interaction: async evidence ingestion trigger (MVP: direct call).
    """

    def before_llm_call(
        self,
        tenant_id: str,
        user_id: str,
        task_type: str | None = None,
        current_instruction: dict[str, Any] | None = None,
        profile_loader: Any | None = None,
    ) -> dict[str, Any]:
        """Emit minimal Response Policy.

        profile_loader: optional callable (tenant_id, user_id, task_type) -> {explicit, task_scores, global_scores}
        If None or fails, falls back to DEFAULT_POLICY merged with current_instruction.
        """
        task_type = validate_task_type(task_type)
        # precedence layer 1: current_instruction (highest, caller already knows)
        cur = current_instruction or {}
        if profile_loader is None:
            # No loader — return default merged with current instruction only
            merged = dict(DEFAULT_POLICY)
            for k in merged:
                if k in cur:
                    merged[k] = cur[k]
            return merged

        try:
            data = profile_loader(tenant_id, user_id, task_type)
            # Expect dict with keys explicit_prefs, task_scores, global_scores (or similar)
            explicit = {}
            task_scores = {}
            global_scores = {}
            if isinstance(data, dict):
                explicit = data.get("explicit_prefs") or data.get("explicit") or {}
                task_scores = data.get("task_scores") or {}
                global_scores = data.get("global_scores") or data.get("trait_scores") or {}
            # Synthesize with full precedence
            policy = synthesize_policy(
                current_instruction=cur,
                explicit_prefs=explicit,
                task_scores=task_scores,
                global_scores=global_scores,
            )
            # Ensure only minimal keys leak
            allowed = set(DEFAULT_POLICY.keys())
            return {k: v for k, v in policy.items() if k in allowed}
        except Exception as e:
            logger.warning(f"AdaptiveProfileHook before_llm_call fallback: {e}")
            # Safe fallback: default + current instruction
            merged = dict(DEFAULT_POLICY)
            for k in merged:
                if k in cur:
                    merged[k] = cur[k]
            return merged

    def format_prompt_injection(self, policy: dict[str, Any]) -> str:
        """Format minimal policy for LLM context injection (no scores)."""
        lines = ["[USER RESPONSE POLICY]"]
        if policy.get("conclusion_first"):
            lines.append("- 결론을 먼저 제시")
        if policy.get("verbosity") == "low":
            lines.append("- 간결하게 응답")
        elif policy.get("verbosity") == "high":
            lines.append("- 충분히 상세하게 응답")
        else:
            lines.append("- 적절한 길이로 응답")
        td = policy.get("technical_depth", "medium")
        if td == "high":
            lines.append("- 기술적 깊이 높게 설명")
        elif td == "low":
            lines.append("- 비기술적/개요 중심으로 설명")
        er = policy.get("evidence_requirement", "medium")
        if er == "high":
            lines.append("- 근거가 필요한 사실은 검증")
        if policy.get("challenge_assumptions"):
            lines.append("- 필요한 경우 기존 가정을 반박")
        alt = policy.get("alternatives", 1)
        if alt > 1:
            lines.append(f"- 핵심 대안은 {alt}개 이하로 제시")
        cl = policy.get("confirmation_level", "medium")
        if cl == "low":
            lines.append("- 불필요한 확인 질문 최소화")
        elif cl == "high":
            lines.append("- 실행 전 확인 질문 포함")
        return "\n".join(lines)


# Singleton for convenience
default_hook = AdaptiveProfileHook()
