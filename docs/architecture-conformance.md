# Architecture Conformance — v1.4.1 (2026-08-27)

> Canonical: `docs/architecture-v1.4.1.md` (3295 lines, SHA `646a8fe`)  
> Previous: v1.3 `4a0383c8` · v1.2 `aac96198` (archived)

## v1.3 → v1.4.1 Delta (new sections)

| Section | Title | Code mapping | Status |
|---------|-------|--------------|--------|
| 16F | Dual Agent Runtime (Safe Default / Hermes Advanced) | `packages/runtime-adapter/runtime_adapter/safe_adapter.py` | ✅ |
| 16F.0 | Runtime Installation & Access (Safe Only / Hermes Only / Both) + Capability `EXECUTE runtime/*` | `registry.py` + `router.py` (5-step selection) | ✅ |
| 16F.1 | Safe Runtime (LLM+MCP, No Shell) | `SafeRuntimeAdapter` — `execute_sandbox` raises `NotImplementedError` | ✅ |
| 16F.2 | Hermes Runtime (Advanced) | `HermesRuntimeAdapter` retained | ✅ |
| 16F.3/16F.4 | Selection Policy / Security Levels L1-L4 | `router.py` + `security_notes.py` | ✅ |
| 16G | Untrusted Execution Worker | `docs/security-model.md` + `security_notes.py` | ✅ |
| 16G.1/16G.2/16G.3 | Capability vs Authority / Shell=Meta / Blast Radius | `security_notes.py` (diagram + constants) | ✅ |
| 16H | Execution Gateway Tool Policy | `execution-gateway/execution_gateway/tool_policy.py` | ✅ |
| 16H.1 | Argument validation (allowed/denied fields, max_results) | `validate_tool_call()` | ✅ |
| 16H.2 | Rate limit per tenant/user/tool | `ToolRateLimiter` (token-bucket) | ✅ |
| 16H.3 | Bulk protection (`is_bulk` threshold 100, BULK_* escalation) | `is_bulk()` | ✅ |
| 16I | Data Access Pattern | `execution-gateway/execution_gateway/data_access.py` | ✅ |
| 16I.1 | Read Path (Read Replica → Query Service → MCP) | `ALLOWED_READ_SOURCES` + `check_read()` | ✅ |
| 16I.2 | Write Path (Command API + Approval) | `check_write()` | ✅ |
| 16I.3 | Direct DB DENY | `check_direct_db()` | ✅ |
| 16J/16K | Runtime Architecture Summary / Design Decision | This doc + `docs/security-model.md` | ✅ |
| 44/45 | 30 decisions + Runtime-Agnostic principle | `ARCHITECTURE_DECISIONS.md` retained | ✅ |

## §16C/16D/16E Carryover (P2)

- §16C 10 contracts: `adapter.py` 6 abstract + `reasoning.py`/`skills.py`/`observability.py`/`context.py` ✅
- §16D: `long_tasks.py`/`concurrency.py`/`metrics.py` + `deploy/k8s/hpa.yaml` (3 HPAs) ✅
- §16E: `AgentRuntimeAdapter` + `register_adapter("safe"/"hermes")` + `control-plane/control_plane/runtime_router.py` shim ✅

## Verification

- `pytest -q`: **180 passed** (172 prior + 8 new: `test_data_access.py` + `tool_policy` indirect)
- Linter: `bash -n` / `python -m py_compile` not required here — `write_file` lint ok
