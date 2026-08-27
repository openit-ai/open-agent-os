"""open-agent-os-runtime-adapter — public package surface."""

from .adapter import AgentRuntimeAdapter
from .factory import get_adapter, register_adapter, ADAPTER_REGISTRY
from .hermes_adapter import HermesRuntimeAdapter, HermesAdapter
from .safe_adapter import SafeRuntimeAdapter, SafeRuntime
from .registry import RuntimeRegistry, RuntimeEntry
from .router import RuntimeRouter
from .reasoning import ReasoningLoop, ReasoningStepResult, ReasoningLoopConfig, SimpleReasoningLoop
from .skills import SkillManifest, SkillRegistry, default_registry as default_skill_registry
from .observability import RuntimeEvent, Span, ObservabilityBus, default_bus as default_observability_bus
from .context import ContextWindow, ContextManager, default_manager as default_context_manager
from .long_tasks import LongTaskManager, LongTask, LongTaskStatus, decompose_task, default_manager as default_long_task_manager

__all__ = [
    "AgentRuntimeAdapter",
    "HermesRuntimeAdapter",
    "HermesAdapter",
    "SafeRuntimeAdapter",
    "SafeRuntime",
    "RuntimeRegistry",
    "RuntimeEntry",
    "RuntimeRouter",
    "get_adapter",
    "register_adapter",
    "ADAPTER_REGISTRY",
    # §16C modules
    "ReasoningLoop",
    "ReasoningStepResult",
    "ReasoningLoopConfig",
    "SimpleReasoningLoop",
    "SkillManifest",
    "SkillRegistry",
    "default_skill_registry",
    "RuntimeEvent",
    "Span",
    "ObservabilityBus",
    "default_observability_bus",
    "ContextWindow",
    "ContextManager",
    "default_context_manager",
    # §16D.2
    "LongTaskManager",
    "LongTask",
    "LongTaskStatus",
    "decompose_task",
    "default_long_task_manager",
]
