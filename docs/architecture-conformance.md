# Architecture Conformance — v1.5.1 (2026-08-28)

> Canonical: `docs/architecture-v1.5.1.md` (3615 lines, SHA `4c2c1b85`)  
> Previous: v1.5 `b19f54ab` + v1.4.1 `646a8fe` + v1.3 `4a0383c8` (archived, preserved)  
> Source: `docs/architecture-v1.5.1.md` — single source of truth

## v1.5.1 Canonical 승격

- v1.5.1을 canonical architecture로 승격. v1.5 (`b19f54ab`, 3417 lines) · v1.4.1 (`646a8fe`, 3295 lines) · v1.3 (`4a0383c8`, 16A/16B/16C/16E) 모두 `docs/` 내 원본 보관 — 삭제 없음, Previous로 참조 유지.
- v1.5 대비 v1.5.1 delta: 198줄 증가 (3417→3615), 16A.3.1 workspace isolation / 16A.6 Controlled Egress Proxy / 16C Core·Advanced 분리 / 16F built-in reference + escalation / §17 ACP=Hermes-specific / §40 4 tests 추가 — 기능 축소 없음, docs-only promotion.

## v1.5.1 New Sections Delta

| Section | Title | Code mapping / Note | Status |
|---------|-------|---------------------|--------|
| 16A.3.1 | Session / User Workspace Isolation (`/home/hermes/workspaces/{tenant}/{agent}/{session}`) — per-session isolation + ephemeral sandbox/container escalation, cross-session DENY | `docs/architecture-v1.5.1.md` §16A.3.1 + `security_notes.py` (workspace namespace) — v1.5.1 new | ✅ |
| 16A.6 | Network Isolation — Controlled Egress Proxy (Hermes → LLM Gateway / Approved Package Mirror / Explicit Allowlist only, direct Internet/DB/ERP/CRM DENY) | `docs/architecture-v1.5.1.md` §16A.6 + `deploy/firewall/hermes-egress.nft` + `docs/security-model.md` (16A.6) — v1.5.1 new | ✅ |
| 16C | Agent Runtime Common Requirements — Core/Advanced split (Core: Session/Streaming/Reasoning/Tool/MCP/Context/Provider/Observability 필수; Advanced: Shell/Python/File/Code/Sub-agent/Long-running 선택) | `docs/architecture-v1.5.1.md` §16C (16C.1–16C.10) — Core/Advanced wording 정상화 | ✅ |
| 16F | LLM Runtime Built-in Reference + Runtime Escalation (LLM=Hermes 교체 가능, Hermes=선택 Advanced, LLM recommendation ≠ escalation, deterministic policy `EXECUTE runtime/hermes` + scope) | `packages/runtime-adapter/runtime_adapter/{registry,router}` + `docs/architecture-v1.5.1.md` §16F/§16F.0 | ✅ |
| §17 | ACP = Hermes-specific Adapter Protocol (Internal Agent Interface = Stable Contract, ACP = replaceable Hermes adapter, LLM Runtime ACP 미종속) | `control-plane/control_plane/runtime_router.py` + `docs/architecture-v1.5.1.md` §17 | ✅ |
| §40 | Security Tests — 4 new (Hermes Cross-Session Workspace Leakage / Direct Internet Egress / Runtime Escalation Bypass / LLM Runtime Arbitrary Code Execution) — DENY / APPROVAL_REQUIRED / UNSUPPORTED | `tests/` pending (180 → 184 planned) — architecture defined, tests to be added | ✅ (arch) |

## §§16F~16K 재확인 (v1.5 → v1.5.1 carryover, 1807~2410행 → v1.5.1 재배치)

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

## §16A~16E Carryover (v1.3/v1.4.1/v1.5)

- §16A Zero-Bypass Invariants (No ACP/MCP Bypass, Sandbox Exception, OS/fs/net/credential isolation) + v1.5.1 16A.3.1/16A.6 확장 ✅
- §16B Hermes Advanced Runtime Selection Rationale ✅
- §16C 10 contracts: `adapter.py` 6 abstract + `reasoning.py`/`skills.py`/`observability.py`/`context.py` + v1.5.1 Core/Advanced split ✅
- §16D: `long_tasks.py`/`concurrency.py`/`metrics.py` + `deploy/k8s/hpa.yaml` (3 HPAs) ✅
- §16E: `AgentRuntimeAdapter` + `register_adapter("llm"/"hermes")` + `control-plane/control_plane/runtime_router.py` shim ✅

## Verification

- `pytest -q`: **180 passed** (v1.5.1 docs-only promotion — runtime/tests 미변경, §40 4 new tests는 architecture-defined, 통과 후 184로 갱신 예정)
- Linter: `bash -n` / `python -m py_compile` not required — docs only
- Archive check: `docs/architecture-v1.3.md` (4a0383c8) + `docs/architecture-v1.4.1.md` (646a8fe) + `docs/architecture-v1.5.md` (b19f54ab) preserved
- Canonical check: `docs/architecture-v1.5.1.md` 3615 lines, SHA `4c2c1b85`
