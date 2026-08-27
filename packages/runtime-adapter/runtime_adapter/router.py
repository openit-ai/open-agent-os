"""RuntimeRouter — §16F 5-step selection + PolicyEngine stub.

5 steps (in order):
  1) Installed check
  2) Enabled check
  3) Capability EXECUTE runtime/safe | runtime/hermes (PolicyEngine/JIT)
  4) Task suitability (shell/python requires hermes)
  5) Resource capability (optional extra gate)

select_runtime(user_id, task_type, required_capability) -> "safe"|"hermes"
"""

from __future__ import annotations

from typing import Any, Callable

from .registry import RuntimeRegistry

# Tasks that require hermes (shell/python)
_HERMES_REQUIRED_TASKS = {"shell", "python", "code_execution", "sandbox", "deploy", "exec"}
_HERMES_KEYWORDS = {"shell", "python", "bash", "exec", "sandbox"}


class RuntimeRouter:
    """5-step router. PolicyEngine integration is stubbed (injectable)."""

    def __init__(
        self,
        registry: RuntimeRegistry | None = None,
        policy_engine: Any | None = None,
        capability_checker: Callable[[str, str], bool] | None = None,
    ):
        self.registry = registry or RuntimeRegistry()
        self.policy_engine = policy_engine
        self.capability_checker = capability_checker

    def _has_capability(self, user_id: str, runtime: str) -> bool:
        """Check EXECUTE runtime/<name> capability.

        Priority: injected checker > PolicyEngine > allow (JIT stub).
        """
        resource = f"runtime/{runtime}"
        if self.capability_checker is not None:
            try:
                return bool(self.capability_checker(user_id, resource))
            except Exception:
                return False
        if self.policy_engine is not None:
            try:
                # Try policy_engine.evaluate with Capability-style request
                from policy_model import PolicyEvaluationRequest  # type: ignore

                req = PolicyEvaluationRequest(
                    user_id=user_id, action="EXECUTE", resource=resource
                )
                result = self.policy_engine.evaluate(req)
                # PolicyDecision.ALLOW == allowed
                return str(getattr(result, "decision", "")).upper() == "ALLOW" or bool(getattr(result, "decision", None) == "ALLOW")
            except Exception:
                # fallback: try generic check
                try:
                    return bool(self.policy_engine.check(user_id, "EXECUTE", resource))
                except Exception:
                    return True  # JIT-friendly fallback
        # No engine configured -> JIT-allow (per spec, JIT possible)
        return True

    def _task_requires_hermes(self, task_type: str, required_capability: str | None) -> bool:
        t = (task_type or "").lower()
        if t in _HERMES_REQUIRED_TASKS:
            return True
        if any(k in t for k in _HERMES_KEYWORDS):
            return True
        rc = (required_capability or "").lower()
        if any(k in rc for k in _HERMES_KEYWORDS):
            return True
        if rc in ("shell", "python", "execute_sandbox"):
            return True
        return False

    def select_runtime(
        self,
        user_id: str,
        task_type: str = "general",
        required_capability: str | None = None,
    ) -> str:
        """5-step selection returning 'safe' or 'hermes'.

        Raises ValueError if no runtime available, PermissionError if capability denied.
        """
        wants_hermes = self._task_requires_hermes(task_type, required_capability)

        # Candidate order: if task needs hermes, prefer hermes; otherwise safe default
        candidates = ["hermes", "safe"] if wants_hermes else ["safe", "hermes"]

        last_deny_reason: str | None = None

        for runtime in candidates:
            # Step 1: installed
            if not self.registry.is_installed(runtime):
                continue
            # Step 2: enabled
            if not self.registry.is_enabled(runtime):
                continue
            # Step 3: Capability EXECUTE runtime/<name>
            if not self._has_capability(user_id, runtime):
                last_deny_reason = f"missing capability EXECUTE runtime/{runtime}"
                continue
            # Step 4: Task suitability
            if runtime == "safe" and wants_hermes:
                # safe cannot handle shell/python
                continue
            # Step 5: Resource capability (extra gate if required_capability references runtime)
            # already covered by step 3; no extra deny here
            return runtime

        if last_deny_reason:
            raise PermissionError(last_deny_reason)
        raise ValueError(f"No available runtime for task_type={task_type!r} (wants_hermes={wants_hermes})")

    # Back-compat alias
    def route(self, user_id: str, task_type: str = "general", required_capability: str | None = None) -> str:
        return self.select_runtime(user_id, task_type, required_capability)
