"""Legacy opencode alias — re-exports opencode_go for backward compat.

opencode (single) was renamed to opencode-go in v1.6.3. This module keeps
`from agent_runtime.providers.opencode import ...` working.
New code should `from agent_runtime.providers.opencode_go import ...`.
"""
from __future__ import annotations

from .opencode_go import (
    OpenCodeProvider,
    resolve_binary_path,
    resolve_project_path,
)

__all__ = ["OpenCodeProvider", "resolve_binary_path", "resolve_project_path"]

# PEP 562: delegate any other attribute (shutil, _is_executable, etc.) to opencode_go
# so that `patch("agent_runtime.providers.opencode.shutil.which")` and similar
# test patches continue to work even though the implementation lives in opencode_go.
def __getattr__(name: str):
    import importlib
    mod = importlib.import_module("agent_runtime.providers.opencode_go")
    return getattr(mod, name)


def __dir__():
    import importlib
    mod = importlib.import_module("agent_runtime.providers.opencode_go")
    return sorted(set(__all__) | set(dir(mod)))
