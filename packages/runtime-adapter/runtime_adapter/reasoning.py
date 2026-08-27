"""Reasoning Loop — §16C.3

Abstract think→act→observe loop with max_steps, tool_calls, termination control.
Concrete runtimes may subclass or adapt via adapter.reasoning_step / loop_until.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


@dataclass
class ReasoningStepResult:
    step: int
    thought: str | None = None
    action: dict[str, Any] | None = None  # {tool, arguments}
    observation: dict[str, Any] | None = None
    done: bool = False
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReasoningLoopConfig:
    max_steps: int = 20
    timeout_s: float | None = None
    retry_on_error: bool = True
    max_retries_per_step: int = 2


class ReasoningLoop(abc.ABC):
    """Abstract reasoning loop — think→act→observe until done."""

    def __init__(self, config: ReasoningLoopConfig | None = None):
        self.config = config or ReasoningLoopConfig()

    @abc.abstractmethod
    async def think(self, session: Any, step: int, history: list[ReasoningStepResult]) -> dict[str, Any]:
        """Produce next thought/action. Return {thought, action:{tool,arguments}, done}."""
        raise NotImplementedError

    @abc.abstractmethod
    async def act(self, session: Any, action: dict[str, Any]) -> dict[str, Any]:
        """Execute tool/action, return observation."""
        raise NotImplementedError

    async def observe(self, session: Any, observation: dict[str, Any]) -> dict[str, Any]:
        """Optional observation post-processing. Default: identity."""
        return observation

    async def is_done(self, step_result: ReasoningStepResult, history: list[ReasoningStepResult]) -> bool:
        """Termination check. Default: step_result.done."""
        return bool(step_result.done)

    async def reasoning_step(
        self,
        session: Any,
        step: int,
        history: list[ReasoningStepResult],
    ) -> ReasoningStepResult:
        thought_action = await self.think(session, step, history)
        done = bool(thought_action.get("done", False))
        thought = thought_action.get("thought")
        action = thought_action.get("action")
        observation: dict[str, Any] | None = None
        error: str | None = None
        if action and not done:
            retries = 0
            while True:
                try:
                    obs = await self.act(session, action)
                    observation = await self.observe(session, obs)
                    break
                except Exception as e:
                    error = str(e)
                    retries += 1
                    if not self.config.retry_on_error or retries > self.config.max_retries_per_step:
                        observation = {"error": error}
                        break
        return ReasoningStepResult(
            step=step,
            thought=thought,
            action=action,
            observation=observation,
            done=done,
            error=error,
            raw=thought_action,
        )

    async def loop_until(
        self,
        session: Any,
        *,
        max_steps: int | None = None,
        done_fn: Callable[[ReasoningStepResult, list[ReasoningStepResult]], bool | Awaitable[bool]] | None = None,
    ) -> dict[str, Any]:
        """Run iterative loop until done or max_steps."""
        limit = max_steps if max_steps is not None else self.config.max_steps
        history: list[ReasoningStepResult] = []
        for step in range(1, limit + 1):
            result = await self.reasoning_step(session, step, history)
            history.append(result)
            # custom done_fn takes precedence
            if done_fn is not None:
                maybe = done_fn(result, history)
                # support async done_fn
                import inspect
                if inspect.isawaitable(maybe):
                    maybe = await maybe  # type: ignore
                if maybe:
                    break
            elif await self.is_done(result, history):
                break
            # error termination: if unrecoverable
            if result.error and not self.config.retry_on_error:
                break
        return {
            "steps": len(history),
            "done": bool(history and history[-1].done) if history else False,
            "history": [
                {
                    "step": r.step,
                    "thought": r.thought,
                    "action": r.action,
                    "observation": r.observation,
                    "done": r.done,
                    "error": r.error,
                }
                for r in history
            ],
        }


class SimpleReasoningLoop(ReasoningLoop):
    """Minimal concrete loop driven by caller-provided callbacks."""

    def __init__(
        self,
        think_fn: Callable[[Any, int, list[ReasoningStepResult]], Any],
        act_fn: Callable[[Any, dict[str, Any]], Any],
        config: ReasoningLoopConfig | None = None,
    ):
        super().__init__(config)
        self._think_fn = think_fn
        self._act_fn = act_fn

    async def think(self, session: Any, step: int, history: list[ReasoningStepResult]) -> dict[str, Any]:
        res = self._think_fn(session, step, history)
        import inspect
        if inspect.isawaitable(res):
            res = await res  # type: ignore
        return res  # type: ignore

    async def act(self, session: Any, action: dict[str, Any]) -> dict[str, Any]:
        res = self._act_fn(session, action)
        import inspect
        if inspect.isawaitable(res):
            res = await res  # type: ignore
        return res  # type: ignore
