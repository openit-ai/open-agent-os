"""Demo — Morning Briefing (Phase 1 Core Personal Agent MVP, Section 3.1)

POST /v1/demo/morning-briefing
  Headers: X-User-Id (employee:kim), X-Tenant-Id optional
  Body: {"tenant_id": "optional"} (or empty)
  Returns: JSON briefing + trace_id (SSE 아님)

- No capability token issuance
- LOW/MEDIUM → allow and return data
- HIGH → APPROVAL_REQUIRED (not executed)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

# Ensure examples/morning-briefing and execution-gateway are importable
ROOT = Path(__file__).resolve().parents[2]
for p in [ROOT / "examples" / "morning-briefing", ROOT / "execution-gateway", ROOT / "security" / "policy-engine", ROOT / "packages" / "policy-model", ROOT / "packages" / "audit-model", ROOT / "packages" / "common-types"]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# Also expose orchestrator via sys.path
for p in [ROOT / "examples"]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    from orchestrator import run_morning_briefing  # type: ignore
except Exception:
    try:
        from morning_briefing.orchestrator import run_morning_briefing  # type: ignore
    except Exception:
        run_morning_briefing = None  # type: ignore

try:
    from execution_gateway.mock_executor import get_ledger
except Exception:
    get_ledger = lambda: None  # type: ignore

from control_plane.session import session_store, new_trace_id, new_request_id  # type: ignore
from control_plane.identity import map_user_to_agent  # type: ignore

router = APIRouter()


class DemoBriefingRequest(BaseModel):
    tenant_id: Optional[str] = None
    message: Optional[str] = None


@router.post("/demo/morning-briefing")
async def morning_briefing(
    request: Request,
    body: DemoBriefingRequest | None = None,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
):
    # 1. Identity — X-User-Id required
    user_id = x_user_id
    # Also allow tenant_id via JSON body or query param
    query_tenant = request.query_params.get("tenant_id")
    tenant_id = None
    if body and body.tenant_id:
        tenant_id = body.tenant_id
    elif x_tenant_id:
        tenant_id = x_tenant_id
    elif query_tenant:
        tenant_id = query_tenant
    else:
        # default tenant from control-plane settings if not provided
        try:
            from control_plane.config import settings
            tenant_id = settings.tenant_id
        except Exception:
            tenant_id = "default"

    if not user_id:
        raise HTTPException(status_code=401, detail="X-User-Id required (employee:...)")

    # Validate identity format
    try:
        mapping = map_user_to_agent(user_id, tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 2. AgentContext with trace_id
    trace_id = new_trace_id()
    request_id = new_request_id()
    agent_context = {
        "tenant_id": tenant_id,
        "user_id": mapping.human_principal,
        "agent_id": mapping.agent_principal,
        "session_id": f"sess_demo_{trace_id[-8:]}",
        "trace_id": trace_id,
        "request_id": request_id,
        "security_domain": mapping.profile.security_domain if hasattr(mapping, "profile") else "general",
    }

    # 3. Run orchestrator (no capability token)
    if run_morning_briefing is None:
        raise HTTPException(status_code=500, detail="orchestrator not available")

    result = await run_morning_briefing(agent_context, tenant_id)

    # 4. Ensure HIGH-risk items are marked APPROVAL_REQUIRED (demo rule: no token issuance)
    # Orchestrator already does this, but double-check enrichment
    # Add explicit top-level trace_id for convenience
    result["trace_id"] = result.get("trace_id", trace_id)

    # Include capability guidance
    for item in result.get("approvals_required", []):
        item.setdefault("note", "HIGH-risk — capability token required; APPROVAL_REQUIRED without token (demo)")

    return result


@router.get("/demo/health")
def demo_health():
    return {"status": "ok", "demo": "morning-briefing", "section": "3.1"}
