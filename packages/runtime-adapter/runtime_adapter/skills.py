"""Skill registry — §16C.6

Load/invoke skill manifests, capability binding. Lightweight in-memory registry
suitable for runtime-adapter; Hermes-specific file loading is out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


@dataclass
class SkillManifest:
    name: str
    version: str = "0.1.0"
    description: str = ""
    capabilities: list[str] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    entrypoint: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillManifest":
        return cls(
            name=str(data.get("name", "")),
            version=str(data.get("version", "0.1.0")),
            description=str(data.get("description", "")),
            capabilities=list(data.get("capabilities") or data.get("tools") or []),
            parameters=dict(data.get("parameters") or {}),
            entrypoint=data.get("entrypoint"),
            raw=dict(data),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "capabilities": self.capabilities,
            "parameters": self.parameters,
            "entrypoint": self.entrypoint,
        }


class SkillRegistry:
    """In-memory skill registry with load/invoke and capability binding."""

    def __init__(self):
        self._skills: dict[str, SkillManifest] = {}
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._capability_index: dict[str, set[str]] = {}  # capability -> skill names

    # ── Registration ─────────────────────────────────────────────────

    def load(self, manifest: SkillManifest | dict[str, Any], handler: Callable[..., Any] | None = None) -> SkillManifest:
        """Load a skill manifest (and optional handler)."""
        m = manifest if isinstance(manifest, SkillManifest) else SkillManifest.from_dict(manifest)
        if not m.name:
            raise ValueError("skill manifest requires 'name'")
        self._skills[m.name] = m
        if handler is not None:
            self._handlers[m.name] = handler
        # index capabilities
        for cap in m.capabilities:
            self._capability_index.setdefault(cap, set()).add(m.name)
        return m

    def unload(self, skill_name: str) -> bool:
        """Remove skill; return True if existed."""
        m = self._skills.pop(skill_name, None)
        self._handlers.pop(skill_name, None)
        if m is None:
            return False
        for cap in m.capabilities:
            s = self._capability_index.get(cap)
            if s:
                s.discard(skill_name)
                if not s:
                    self._capability_index.pop(cap, None)
        return True

    def get(self, skill_name: str) -> SkillManifest | None:
        return self._skills.get(skill_name)

    def list(self) -> list[SkillManifest]:
        return list(self._skills.values())

    def list_dicts(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self._skills.values()]

    def bind_handler(self, skill_name: str, handler: Callable[..., Any]) -> None:
        if skill_name not in self._skills:
            raise KeyError(f"skill not loaded: {skill_name}")
        self._handlers[skill_name] = handler

    # ── Capability binding ───────────────────────────────────────────

    def skills_for_capability(self, capability: str) -> list[str]:
        return sorted(self._capability_index.get(capability, set()))

    def has_capability(self, capability: str) -> bool:
        return capability in self._capability_index

    # ── Invoke ───────────────────────────────────────────────────────

    async def invoke(
        self,
        skill_name: str,
        action: str | None = None,
        params: dict[str, Any] | None = None,
        session: Any | None = None,
    ) -> dict[str, Any]:
        """Invoke a skill handler or return manifest stub."""
        m = self._skills.get(skill_name)
        if m is None:
            raise KeyError(f"skill not found: {skill_name}")
        handler = self._handlers.get(skill_name)
        if handler is None:
            # stub: no handler bound — return manifest + params echo
            return {"skill": skill_name, "action": action, "params": params or {}, "manifest": m.to_dict(), "status": "no_handler"}
        # call handler (sync or async)
        import inspect
        kwargs: dict[str, Any] = {"skill": skill_name, "action": action, "params": params or {}, "manifest": m, "session": session}
        # handler signature flexible: try kwargs, fallback to single dict
        try:
            res = handler(**kwargs)  # type: ignore
        except TypeError:
            res = handler({"skill": skill_name, "action": action, "params": params or {}, "session": session})  # type: ignore
        if inspect.isawaitable(res):
            res = await res  # type: ignore
        if isinstance(res, dict):
            return res
        return {"result": res}


# Module-level default registry (convenience)
default_registry = SkillRegistry()
