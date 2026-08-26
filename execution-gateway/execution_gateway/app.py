from fastapi import FastAPI
app = FastAPI(title="Open Agent OS — Execution Gateway", version="1.1.0")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/v1/tools")
def list_tools():
    return {"tools": []}
