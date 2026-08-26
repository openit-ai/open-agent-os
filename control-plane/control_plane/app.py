"""FastAPI entrypoint for Control Plane."""
from fastapi import FastAPI
from .config import settings

app = FastAPI(title="Open Agent OS — Control Plane", version="1.1.0")

@app.get("/health")
def health():
    return {"status": "ok", "tenant": settings.tenant_id}

@app.get("/v1/agent-context/validate")
def validate_context_example():
    # placeholder — real validation via agent_context.AgentContext
    return {"ok": True}
