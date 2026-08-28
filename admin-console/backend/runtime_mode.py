"""Runtime mode selector — Hermes vs LLM Runtime.

GET  /v1/runtime/mode — returns current mode
POST /v1/runtime/mode — sets mode (L5 only), body {mode: "hermes"|"llm"}

Persisted in-memory + env fallback. When OAOS_RUNTIME_MODE env is set, it initializes from there.
Hermes uses internal LLM via Hermes Agent, so no external provider config needed.
LLM Runtime requires multi-provider config (claude/codex/gemini/opencode-go/openrouter/ollama).
"""
from __future__ import annotations

import os
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

try:
    from .auth import AdminUser, get_current_admin, require_l5
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

router = APIRouter(prefix="/v1/runtime", tags=["runtime"])


class RuntimeMode(str, Enum):
    hermes = "hermes"
    llm = "llm"


# In-memory state, initialized from env if present
_current_mode: RuntimeMode = RuntimeMode(os.environ.get("OAOS_RUNTIME_MODE", "hermes").lower()) if os.environ.get("OAOS_RUNTIME_MODE", "").lower() in ("hermes", "llm") else RuntimeMode.hermes


def get_mode() -> RuntimeMode:
    return _current_mode


def set_mode(m: RuntimeMode) -> RuntimeMode:
    global _current_mode
    _current_mode = m
    # also persist to env for process
    os.environ["OAOS_RUNTIME_MODE"] = m.value
    return _current_mode


class ModeRequest(BaseModel):
    mode: RuntimeMode


@router.get("/mode")
def get_runtime_mode(admin: AdminUser = Depends(get_current_admin)):
    return {"mode": _current_mode.value, "available_modes": [e.value for e in RuntimeMode]}


@router.post("/mode")
def post_runtime_mode(body: ModeRequest, admin: AdminUser = Depends(require_l5)):
    new_mode = set_mode(body.mode)
    return {"mode": new_mode.value, "available_modes": [e.value for e in RuntimeMode], "updated_by": admin.email}
