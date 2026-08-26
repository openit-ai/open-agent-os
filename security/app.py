from fastapi import FastAPI
app = FastAPI(title="Open Agent OS — Security & Governance", version="1.1.0")
@app.get("/health")
def health():
    return {"status": "ok"}
@app.get("/v1/policy/evaluate")
def policy_evaluate(): return {"decision": "DENY"}
