# Architecture Conformance — v1.5 (2026-08-28)

> Canonical: `docs/architecture-v1.5.md` (3417 lines, SHA `b19f54ab`)  
> Previous: v1.4.1 `646a8fe` · v1.3 `4a0383c8` (archived, preserved)  
> Source: `docs/architecture-v1.5.md` — single source of truth

## v1.5 Canonical 승격

- v1.5를 canonical architecture로 승격. v1.3 (`4a0383c8`, 16A/16B/16C/16E) 및 v1.4.1 (`646a8fe`, 3295 lines) 모두 `docs/` 내 원본 보관 — 삭제 없음, Previous로 참조 유지.
- v1.4.1 대비 v1.5 delta: 122줄 증가 (3295→3417), 16F 명칭 정비 `Safe Runtime → LLM Runtime` (Dual Runtime 용어 통일), §§16F~16K 구조·시맨틱 재확인 — 기능 축소 없음.

## §§16F~16K 재확인 (v1.5 기준, 1807~2410행)

| Section | Title | Code mapping | Status |
|---------|-------|--------------|--------|
| 16F | LLM Runtime / Hermes Runtime Dual Architecture | `packages/runtime-adapter/runtime_adapter/` + `control-plane/control_plane/runtime_router.py` | ✅ 재확인 |
| 16F.0 | Runtime Installation & Access Policy (LLM Only / Hermes Only / Both) + Capability `EXECUTE runtime/*` | `registry.py` + `router.py` (5-step selection) | ✅ |
| 16F.1 | LLM Runtime (LLM+MCP, No Shell) | `LLMRuntimeAdapter` — `execute_sandbox` raises `NotImplementedError` (ex-SafeRuntimeAdapter) | ✅ |
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
| 16J/16K | Runtime Architecture Summary / Design Decision | This doc + `docs/security-model.md` (16J revised summary, 16K decision: LLM default / Hermes advanced) | ✅ |
| 44/45 | 30 decisions + Runtime-Agnostic principle | `ARCHITECTURE_DECISIONS.md` retained | ✅ |

## §16A~16E Carryover (v1.3/v1.4.1)

- §16A Zero-Bypass Invariants (No ACP/MCP Bypass, Sandbox Exception, OS/fs/net/credential isolation) ✅
- §16B Hermes Advanced Runtime Selection Rationale ✅
- §16C 10 contracts: `adapter.py` 6 abstract + `reasoning.py`/`skills.py`/`observability.py`/`context.py` ✅
- §16D: `long_tasks.py`/`concurrency.py`/`metrics.py` + `deploy/k8s/hpa.yaml` (3 HPAs) ✅
- §16E: `AgentRuntimeAdapter` + `register_adapter("llm"/"hermes")` + `control-plane/control_plane/runtime_router.py` shim ✅

## Verification

- `pytest -q`: **180 passed** (v1.4.1 기준 — `test_data_access.py` + `tool_policy` 포함, v1.5 문서 변경으로 코드 변경 없음)
- Linter: `bash -n` / `python -m py_compile` not required — docs only
- Archive check: `docs/architecture-v1.3.md` (4a0383c8) + `docs/architecture-v1.4.1.md` (646a8fe) preserved
