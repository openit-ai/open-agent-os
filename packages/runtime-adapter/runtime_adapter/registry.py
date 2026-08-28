"""RuntimeRegistry — §16F Dual Runtime registry with YAML load.

runtimes dict: safe/hermes with installed/enabled/security_level.
YAML load + is_available() + helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RuntimeEntry:
    name: str  # safe | hermes
    installed: bool = True
    enabled: bool = True
    security_level: str = "standard"  # standard | privileged
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "installed": self.installed,
            "enabled": self.enabled,
            "security_level": self.security_level,
            "description": self.description,
            **self.extra,
        }


_DEFAULT_RUNTIMES: dict[str, dict[str, Any]] = {
    "llm": {"installed": True, "enabled": True, "security_level": "standard", "description": "LLM Runtime (Standard / Controlled)"},
    "safe": {"installed": True, "enabled": True, "security_level": "standard", "description": "Safe Default (LLM+MCP) — deprecated alias for llm"},
    "hermes": {"installed": True, "enabled": True, "security_level": "privileged", "description": "Advanced (Shell/Python)"},
}


class RuntimeRegistry:
    """Registry of available runtimes.

    Args:
        runtimes: dict name -> config (installed/enabled/security_level/...)
        yaml_path: optional YAML file to load (overrides defaults).

    YAML shape:
        runtimes:
          safe:   {installed: true, enabled: true, security_level: standard}
          hermes: {installed: true, enabled: false, security_level: privileged}
    """

    def __init__(
        self,
        runtimes: dict[str, dict[str, Any]] | None = None,
        yaml_path: str | Path | None = None,
    ):
        base = {k: dict(v) for k, v in _DEFAULT_RUNTIMES.items()}
        if runtimes:
            for k, v in runtimes.items():
                base[k.lower()] = dict(v)
        self.runtimes: dict[str, RuntimeEntry] = {}
        for name, cfg in base.items():
            self.runtimes[name] = RuntimeEntry(
                name=name,
                installed=bool(cfg.get("installed", True)),
                enabled=bool(cfg.get("enabled", True)),
                security_level=str(cfg.get("security_level", "standard")),
                description=str(cfg.get("description", "")),
                extra={k: v for k, v in cfg.items() if k not in ("installed", "enabled", "security_level", "description")},
            )
        # v1.5 §16E.6: llm canonical, safe deprecated alias — unify with explicit-caller priority
        caller_keys = {k.lower() for k in (runtimes or {}).keys()}
        if "safe" in caller_keys and "llm" not in caller_keys:
            # caller used deprecated safe key only → propagate to llm
            self.runtimes["llm"] = self.runtimes["safe"]
            self.runtimes["safe"] = self.runtimes["llm"]
        elif "llm" in self.runtimes:
            self.runtimes["safe"] = self.runtimes["llm"]
        if yaml_path is not None:
            self.load_yaml(yaml_path)

    def load_yaml(self, path: str | Path) -> None:
        """Load/merge YAML file into registry. Missing file is no-op."""
        p = Path(path)
        if not p.exists():
            return
        try:
            import yaml  # type: ignore
        except ImportError:
            # minimal fallback: try json
            import json

            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return
            self._merge_data(data)
            return
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            return
        self._merge_data(data)

    def _merge_data(self, data: dict[str, Any]) -> None:
        runtimes_data = data.get("runtimes", data)
        if not isinstance(runtimes_data, dict):
            return
        for name, cfg in runtimes_data.items():
            if not isinstance(cfg, dict):
                continue
            key = name.lower()
            # alias: safe <-> llm (v1.5 rename, §16E.6)
            if key == "safe":
                key = "llm"
            if key not in self.runtimes:
                self.runtimes[key] = RuntimeEntry(name=key)
            entry = self.runtimes[key]
            if "installed" in cfg:
                entry.installed = bool(cfg["installed"])
            if "enabled" in cfg:
                entry.enabled = bool(cfg["enabled"])
            if "security_level" in cfg:
                entry.security_level = str(cfg["security_level"])
            if "description" in cfg:
                entry.description = str(cfg["description"])
        # keep deprecated safe alias in sync with llm
        if "llm" in self.runtimes:
            self.runtimes["safe"] = self.runtimes["llm"]

    def is_available(self, name: str) -> bool:
        """Available == installed and enabled."""
        entry = self.runtimes.get(name.lower())
        if entry is None:
            return False
        return entry.installed and entry.enabled

    def is_installed(self, name: str) -> bool:
        e = self.runtimes.get(name.lower())
        return bool(e and e.installed)

    def is_enabled(self, name: str) -> bool:
        e = self.runtimes.get(name.lower())
        return bool(e and e.enabled)

    def get(self, name: str) -> RuntimeEntry | None:
        return self.runtimes.get(name.lower())

    def list_runtimes(self) -> list[RuntimeEntry]:
        return list(self.runtimes.values())

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {k: v.to_dict() for k, v in self.runtimes.items()}

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RuntimeRegistry":
        return cls(yaml_path=path)
