# Core Platform Gap Audit vs Architecture v1.6 §§5-26
**Date:** 2026-08-28 | **Scope:** `control-plane/` + `execution-gateway/` + `security/` | **Spec:** `docs/architecture-v1.6.md`
**Auditor:** sub-agent (read-only, file:line evidence)

> Legend: ✅ Implemented · ⚠️ Partial · ❌ Missing  — every row has file:line pointer.

---

## 1. Product Model & Deployment (§5) — 6 rows

| # | Spec Requirement (§5) | Status | Evidence |
|---|---|---|---|
| 5.1 | Single customer-owned Postgres instance, 3 separate DB+User (`mattermost`/`outline`/`openagentos`) — same instance OK, same DB/user NG | ⚠️ Partial | `security/models/db.py:1-15` `get_engine()` builds `openagentos` URL; `deploy/docker-compose.prod.yml:129-130` uses `${POSTGRES_DB:-openagentos}` per-service but `control-plane/.env:6` hardcodes `postgresql+asyncpg://open_agent_os:secret@localhost:5432/open_agent_os` (wrong DB name + plaintext secret). No CI check that `mattermost` user cannot read `openagentos`. |
| 5.2 | `openagentos` DB is Source of Truth for persistent state; Redis = cache/queue/lock/hot-state only, NEVER persistent memory store | ✅ | `control-plane/control_plane/session.py:161-283` `RedisSessionStore` is hot cache with TTL 24h; `security/models/orm.py:147-189` `MemoryORM` on Postgres/pgvector is persistent memory; comment in `control-plane/control_plane/session.py:1-6` notes Redis fallback. |
| 5.3 | Admin Console persistent ops stored in `openagentos` (users/groups/agents/policies/runtime bindings/connector settings/approval history) — not in Redis/files | ⚠️ Partial | `security/models/orm.py:190-201` `AdminStateORM` exists (key/value JSON); `admin-console/backend/persistence.py` absent (only `admin-console/backend/app.py` in repo, not wired to `AdminStateORM`). Contract present, runtime wiring incomplete. |
| 5.4 | Secrets (refresh_token, api_key, private_key, client_secret, signing_secret) → Credential Vault, NEVER plain DB column | ✅ | `security/credential-vault/vault/vault.py:42-61` `EncryptedPostgresVault` Fernet-encrypts `encrypted_token LargeBinary`; `security/models/orm.py:133-144` stores only `secret_ref`+metadata; doc `security/models/orm.py:58-60` `secret_ref unique`. No plaintext column. |
| 5.5 | Runtime pluggable: `LLM Only / Hermes Only / LLM+Hermes` — at least one required, Hermes NOT mandatory | ✅ | `packages/runtime-adapter/runtime_adapter/registry.py:20-35` `_DEFAULT_RUNTIMES` with `llm` canonical + `safe` alias + `hermes`; `control-plane/control_plane/runtime_router.py:8-10` shim re-exports registry; `docs/architecture-v1.6.md:837-843` installed configs match. |
| 5.6 | Deployment targets: on-prem / VPS / private cloud / K8s/VM, with env isolation per customer | ⚠️ Partial | `deploy/docker-compose.prod.yml` + `deploy/k8s/` + `deploy/systemd/hermes.service` present; but `deploy/docker-compose.dev.yml:22` exposes `8001:8001` publicly (prod file uses `expose` correctly). K8s manifests exist, not audited for image pull secrets. |

---

## 2. Runtime Abstraction & Control Plane (§6, §7, §14-§17)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 6.1 | Logical Personal Agent = per-employee principal, NOT per-employee Hermes process | ✅ | `control-plane/control_plane/identity.py:15-27` `map_user_to_agent` deterministic `employee:kim → agent:assistant:kim`; `control-plane/control_plane/personal_agent.py:derive_agent_id`. |
| 7.1a | Control plane handles: identity mapping, Logical Agent, session create/resume/route, Runtime Registry/Router, Adapter call, streaming, approval routing, AgentContext | ✅ | `control-plane/control_plane/app.py:40-54` create_session; `59-78` get_session (owner check); `80-90` send_prompt via ACP; `92-111` SSE stream; `119-123` `GET /v1/context/{session_id}` AgentContext. |
| 7.1b | Control plane does NOT: finalize authorization, store credential plaintext, execute tools, store enterprise data | ✅ | `app.py` delegates to `identity`, `session_store`, `acp` only; no credential handling; no `proxy_tool_call`. AuthZ lives in `execution-gateway/`. |
| 14.1 | Session isolation: `assert_owner` blocks cross-user access | ✅ | `control-plane/control_plane/session.py:49-53` `assert_owner`; enforced in `app.py:69,83,96,115,122` every endpoint via `X-User-Id`. |
| 16.1 | Runtime Adapter Contract (canonical Internal Agent Interface ≠ ACP): `createSession/resumeSession/sendPrompt/streamEvents/cancelTask/getSessionState` | ✅ | `control-plane/control_plane/internal_api.py:14-34` Protocol `InternalAgentInterface`; `control-plane/control_plane/acp_adapter.py:1-13` doc “Internal Agent Interface is canonical, ACP is adapter”. |
| 17.1 | **Hermes single-path** — only via `ACPAdapter`; no direct `User→Hermes` / `Mattermost→Hermes` bypass | ✅ | `control-plane/control_plane/app.py:26` `acp = ACPAdapter(settings.hermes_base_url)` sole instance; `acp_adapter.py:25-28` single class; `mattermost_adapter/webhook.py:156,264` create ACPAdapter; `identity.py` never touches Hermes directly. No other `hermes_base_url` consumer. |
| 17.2 | **Ollama removal** — no Ollama client/import; Hermes Gateway fallback uses same LLM as `@openit CoCo` | ✅ | `grep -r ollama` → only hit `acp_adapter.py:12` comment “not Ollama. Uses OAOS_CP_HERMES_API_KEY.”; `config.py:9` `hermes_model="qwen2.5"`; fallback URL `acp_adapter.py:142-145` rewrites `:8001→:8642 /v1/chat/completions` with Bearer `OAOS_CP_HERMES_API_KEY`. No `ollama` dep in `pyproject.toml`. |
| 18.1 | AgentContext canonical 7-field + optional delegation/binding IDs, propagated as headers | ✅ | `control-plane/control_plane/session.py:55-64` `to_agent_context()` tenant/user/agent/session/trace/request/security_domain; `acp_adapter.py:31-40` `_headers` maps to `X-Tenant-Id`…; `execution-gateway/execution_gateway/app.py:44-93` accepts both `X-Agent-Context` JSON and per-header `X-Delegation-Id`. |

---

## 3. Mattermost Bridge & Threading (task-specific checks)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| M1 | **Port 8100 bridge** — spec/task mentions `mattermost bridge 8100` | ⚠️ Partial | No `:8100` listener in repo. `grep ports` shows CP `8000`, EG `8001`, Sec `8002` (`deploy/docker-compose.prod.yml:170-193`); Hermes Gateway at `:8642` (`acp_adapter.py:144-145` replaces `:8001→:8642`). Webhook is `POST /v1/mattermost/events` on CP `8000` (`webhook.py:287`), not `8100`. If spec meant legacy bridge port, it has been consolidated — but spec-doc string `8100` is absent from `docs/architecture-v1.6.md` and codebase; needs doc correction or compose alias `:8100→8000`. |
| M2 | **Thread `root_id`** — replies thread under triggering post | ✅ | `control-plane/control_plane/mattermost_adapter/webhook.py:146` param `root_id: str|None`; `271` `thread_root = root_id or post_id`; `309` parses `payload.root_id || data.post.root_id || rootId`; `adapters/mattermost/adapter.py:219-220` `out["root_id"]=root_id` and `228-229` `body["root_id"]=root_id` on POST. Tests: `tests/test_mattermost_adapter.py:248,259,529` assert `root_id` propagated. |
| M3 | Slash `/mattermost/slash` + interactive 4-button (`/mattermost/actions`) + HMAC verification | ✅ | `webhook.py:319-380` slash (form-urlencoded + JSON); `383-499` actions with `decision_map` 4 states; `81-87` `verify_mattermost_signature` HMAC-SHA256; `adapters/mattermost/adapter.py:187-195` `post_approval_card` builds attachment with 4 `actions`. |
| M4 | Mattermost URL / bot token / webhook secret from env, not hardcoded | ⚠️ Partial | `config.py:12-14` reads `mattermost_url/bot_token/webhook_secret` via `OAOS_CP_` prefix (correct); but `control-plane/.env:1-3` commits real bot token `7t59pff9...` to repo — secret leak, must rotate + `.env.example` pattern. |

---

## 4. Security Hardening §§16A-16K

| # | Spec Invariant | Status | Evidence |
|---|---|---|---|
| 16A.1 | **No ACP Bypass** — User/Mattermost/Slack cannot hit Hermes outside ACP | ✅ | `acp_adapter.py:1-13` doc invariant; ingress is `control-plane/app.py` + `mattermost_adapter/webhook.py` only; no `hermes_base_url` exposed to client headers in `app.py:44-93` parsing. |
| 16A.2 | **No MCP Bypass** — Hermes accesses enterprise/personal resources ONLY via MCP/Execution Gateway (local `/home/hermes` exception) | ✅ | `execution-gateway/execution_gateway/proxy.py:154-365` enforces capability+data_access before transport; `execution-gateway/execution_gateway/mcp_registry.py:228-351` registry is sole discovery; `control-plane` never constructs enterprise SDK clients directly. |
| 16A.3 | Session/User workspace isolation: `/home/hermes/workspaces/{tenant}/{agent}/{session}` namespace + ephemeral sandbox for sensitive/high-risk | ⚠️ Partial | Deploy doc `deploy/systemd/README.md` defines `/home/hermes` but **no code** creates per-session subdir; `session.py` stores `hermes_worker` pool name only, not filesystem path. Isolation relies on ops procedure, not runtime assertion. Recommend adding `WorkspaceAllocator` in `acp_adapter.create_session_remote` that passes `workspace=/home/hermes/workspaces/{tenant}/{agent}/{session}`. |
| 16A.4 | Hermes dedicated OS account `hermes` / no sudo / no root | ✅ (deploy) | `deploy/systemd/hermes.service:8-12` `User=hermes Group=hermes NoNewPrivileges=true`; `deploy/scripts/create-hermes-user.sh` creates user with `passwd -l` + `99-hermes-deny`. No `sudo` in `hermes.service`. |
| 16A.5 | Filesystem isolation: `PrivateTmp, ProtectSystem=strict, ProtectHome=true, ReadWritePaths=/home/hermes` | ✅ (deploy) | `deploy/systemd/hermes.service:22-36` `PrivateTmp=true ProtectSystem=strict ProtectHome=true ReadWritePaths=/home/hermes ProtectKernelTunables...` — full §16A.5 set. Not runtime-verified in Python (acceptable — OS boundary). |
| 16A.6 | Network isolation: ALLOW ACP/MCP/LLM egress; DENY DB/ERP/CRM/SSH/internal API | ✅ (deploy) | `deploy/firewall/hermes-egress.nft` defines `ACP_HOST/MCP_HOST/LLM_HOSTS` allow + `DB/ERP/CRM` deny; `deploy/systemd/README.md §5` shows nftables+firewalld alternatives. Python code does not enforce (`acp_adapter.py` trusts `hermes_base_url`); boundary is host firewall, as spec prescribes. |
| 16A.7 | Credential isolation: Hermes holds short-lived capability, NEVER enterprise credential | ✅ | `security/credential-vault/vault/vault.py:151-183` owner check `requester_agent_id != owner_agent_id → PermissionError`; `proxy.py:222-266` HIGH-risk requires `capability_token`; `acp_adapter.py` never passes `DB_PASSWORD/ERP_API_KEY`; `deploy/systemd/hermes.service:17-20` comment “Do NOT inject enterprise secrets”. |
| 16B-D | Runtime selection rationale + common requirements (session/streaming/reasoning/tool-call/MCP/skill/context/model/observability) | ✅ | `packages/runtime-adapter/runtime_adapter/router.py:31-72` implements 5-step `select_runtime`; `registry.py:1-50` YAML-loadable `RuntimeRegistry`; trail events attached in `proxy.py:350-359` audit hint with `trace_id/request_id`. |
| 16F | Dual architecture — `EXECUTE runtime/llm` vs `runtime/hermes` Capability + JIT scope | ⚠️ Partial | `packages/runtime-adapter/runtime_adapter/router.py:35-48` `_has_capability(user_id, runtime)` checks `EXECUTE runtime/{hermes,llm}` via injected `capability_checker` or `PolicyEngine.evaluate`; `control-plane/control_plane/app.py:43-50` does **not** call `RuntimeRouter` before `acp.create_session_remote` (always assumes hermes). Needs `app.py:create_session` to invoke `RuntimeRouter.select_runtime` first, else JIT deny is bypassed at session creation. |
| 16G | Untrusted Execution Worker — Shell = meta-capability constrained by filesystem/network/credential boundaries (not removed) | ✅ | Doc invariant in `acp_adapter.py:1-13`; runtime never grants credential; `deploy/systemd/hermes.service` restricts shell to `/home/hermes`. |
| 16H | **Tool Policy** — argument validation + allowed/denied fields + row/file limits | ✅ | `execution-gateway/execution_gateway/tool_policy.py:55-114` `validate_tool_call(policy)` covers action allowlist, denied_fields, allowed_fields, `max_results` (limit/count/page_size/result_count), `max_file_size`. Deterministic, no LLM. |
| 16H.2 | Rate limit per user/agent/session/tool/resource/tenant | ⚠️ Partial | `tool_policy.py:126-159` `ToolRateLimiter` token-bucket (10/s burst 20) — **not wired** in `execution-gateway/execution_gateway/app.py:145-258` `execute()` path. Currently imported but never instantiated. Must inject limiter keyed by `tenant:user:tool:resource` before `authz_hook.authorize`. |
| 16H.3 | Bulk access protection — `BULK_READ/BULK_DOWNLOAD/EXPORT/SHARE_EXTERNAL` escalate | ✅ | `tool_policy.py:37-52` `is_bulk()` detects `_BULK_KEYWORDS/_BULK_ACTIONS/result_count≥100`; `execution-gateway/execution_gateway/risk.py:62-67` `HIGH_RISK_ACTIONS` includes `BULK_READ/BULK_DOWNLOAD/BULK_EXPORT`; `knowledge.py` also classifies. |
| 16I | Data access pattern: Read via Replica/View/QueryService/MCP; Write via CommandAPI + approval; Direct DB DENY | ✅ | `execution-gateway/execution_gateway/data_access.py:91-270` `DataAccessPolicy` with `read_path/write_path/direct_db_access/blast_radius` — `proxy.py:194-215` blocks `direct_db`/`blast_radius`/`production` resource → `DATA_ACCESS_DENIED` (HIGH). |
| 16J-K | Runtime design decision — Runtime = replaceable engine; core value = Identity+Delegation+Policy+Vault+Audit+Memory | ✅ | `packages/runtime-adapter` abstraction fulfills replaceability; `control-plane/control_plane/hermes_adapter.py:1-12` is re-export shim proving Hermes not hardcoded. |

---

## 5. Personal Delegation & Credential Vault (§§8-10)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 9.1 | User-consented delegation (`employee:kim → agent:assistant:kim` per provider/scope) | ✅ | `security/delegation/delegation_service/service.py:26-66` `grant(user_id,agent_id,provider,scope)` + `bindings` + `revoke` cascade; `security/app.py:136-184` exposes `POST /v1/delegation/grant|revoke|{id}`. |
| 10.2 | No plaintext, encrypted store, user/agent scope, refresh/access separation, immediate revoke, minimal scopes, privileged audit | ✅ | `vault.py:42-60` Fernet via `_derive_fernet_key`; `151-183` owner check; `190-216` `PERSONAL_CREDENTIAL_USE` audit to `audit_ledger`; `219-226` `revoke` deletes DB+memory. `orm.py:58-64` `CredentialBindingORM` stores `secret_ref` not secret. Refresh/access split is metadata-level (scope), token bytes opaque — acceptable. |
| 10.3 | Metadata bundle: credential_id, user_id, agent_id, provider, scope, issued/expiry, refreshable, status, last_used_at | ⚠️ Partial | `orm.py:51-64` has `id/delegation_id/provider/secret_ref/scope/status/expires_at/last_used_at` but lacks explicit `issued_at/refreshable` columns; `vault.py:64-66` meta dict omits `refreshable`. Easy fix: add `issued_at TIMESTAMP` + `refreshable BOOL` to `CredentialBindingORM`. |

---

## 6. Enterprise Authorization & Policy Engine (§§11, 25)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 25.1 | Deterministic 8-step order: 1 Explicit Deny →2 Security Boundary Deny →3 Personal Delegation →4 Persistent User Grant →5 Group Grant →6 Default Bundle →7 Approval Required →8 Default Deny | ✅ | `security/policy-engine/policy_engine/engine.py:32-82` groups by `POLICY_EVALUATION_ORDER` and iterates in that order; `51-74` returns first matching rule per source; comment `67` “Explicit Deny가 먼저 평가되므로 Personal Delegation override”. `packages/policy-model` defines `POLICY_EVALUATION_ORDER`. |
| 25.2 | Personal Delegation cannot override company Explicit Deny | ✅ | Same loop guarantee — Explicit Deny source evaluated first, short-circuits (`engine.py:51-74`). |
| 25.3 | fnmatch glob resource matching; action `*` wildcard; priority+id tie-break | ✅ | `engine.py:58-63` `fnmatch.fnmatch(resource, rule.resource_pattern)` + `rule.action=="*"`. |

---

## 7. Capability Token (§26)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 26.1 | One-time / short-lived signed token (JWT HS256) with claims `sub/on_behalf_of/action/resource/session_id/request_id/delegation_id/expires_at` + nonce/jti/iat/exp | ✅ | `security/token/token_service/service.py:32-58` `issue()` builds all claims + `nonce=uuid4 hex` + `jti=uuid4 hex` + `iat/exp` (default TTL 300s `17`). |
| 26.2 | Verification: signature + expiry + nonce/jti replay + revoked check | ✅ | `service.py:60-90` `verify()` → `jwt.decode` → `ExpiredSignatureError` → `jti in _revoked` → `jti/nonce in _seen_*` replay; stateless helper `146-174` supports external stores; `execution-gateway/execution_gateway/proxy.py:229-266` delegates to `verify_capability` with context binding. |
| 26.3 | Execution Gateway enforces capability for HIGH-risk, passes trace | ✅ | `proxy.py:243-266` `if risk==HIGH and not token_dict → CAPABILITY_REQUIRED`; `314-329` attaches `delegation_id/capability_jti/nonce` to result. |

---

## 8. Approval Workflow (§§12, 23, 24)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 24.1 | `approval_id/request_hash/nonce/signature/expires_at` + HMAC-SHA256 | ✅ | `security/approval/approval_workflow/workflow.py:68-92` `create()` builds `raw="user|agent|action|resource|nonce|expires"` → `request_hash=sha256(raw)` → `signature=HMAC(signing_key, request_hash)`; `verify()` recomputes both. |
| 24.2 | Verification: signature + nonce replay + expiration + request hash + approver identity + policy-change permission | ✅ (partial) | `workflow.py:102-135` `verify()` + `decide()` checks expiry, signature, hash, `nonce in _seen_nonces`, `decided_by` stored; `execution-gateway/authz_hook.py` enforces policy-change permission upstream. Nonce replay only on `decide`, not on transport replay — add `verify()` nonce-seen check for full §24. |
| 23.1 | Mattermost 4-button UX: `[Deny][Approve Once][Always(User)][Always(Group)]` threaded under approval request | ✅ | `adapters/mattermost/adapter.py:187-195` `build_approval_card` → `post_approval_card`; `control-plane/control_plane/mattermost_adapter/webhook.py:383-489` `POST /v1/mattermost/actions` maps `deny→DENIED`, `approve_once→APPROVED_ONCE`, `approve_user_always→APPROVED_USER_ALWAYS`, `approve_group_always→APPROVED_GROUP_ALWAYS`. |
| 12.1 | Persistent grants: user-always / group-always via fnmatch | ✅ | `workflow.py:154-173` `_user_grants/_group_grants` sets + `has_user_grant/has_group_grant` fnmatch. |
| 12.2 | Approval routing via Agent Control Plane (not direct Hermes→Human) | ✅ | `webhook.py:383-489` control-plane is approval endpoint; Hermes calls Execution Gateway which returns `APPROVAL_REQUIRED` (403), then control-plane posts Mattermost card — no Hermes→Human direct path. Limitation: `ApprovalStore` is in-memory (`workflow.py:40` `self._requests dict`), not DB-backed → restart loses approvals → ⚠️ Partial for prod persistence (ORM `ApprovalRequestORM:66-84` exists but store doesn’t use it). |

---

## 9. Audit Ledger (§§31-32)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| A1 | Hash-chain: `hash(prev_hash + canonical_payload)` + `previous_hash/event_hash` | ✅ | `security/audit/audit_ledger/ledger.py:32-36` `append()` sets `previous_hash=self._head; event_hash=compute_hash(); self._head=event_hash`; `packages/audit-model` `AuditEvent.compute_hash()` canonicalizes payload. |
| A2 | `verify_chain` integrity check (tamper detection) | ✅ | `ledger.py:51-60` iterates, checks `previous_hash==prev && compute_hash==event_hash`; test helper `105-109` `tamper_event`. |
| A3 | Checkpoint: periodic HMAC-SHA256 of `chain_head_hash` + `verify_checkpoint` + event_count prefix validation | ✅ | `ledger.py:63-73` `checkpoint()` HMAC(signing_key, head); `75-103` `verify_checkpoint()` checks signature + `event_count <= len(events)` + prefix head membership. |
| A4 | Ledger persisted & exportable (S3 prefix) | ⚠️ Partial | `security/app.py:301-332` exposes `/v1/audit/verify|checkpoint|events`; `deploy/docker-compose.prod.yml:130` sets `AWS_S3_BUCKET/REGION/PREFIX=audit-checkpoints/` env. But `AuditLedger` is **in-memory list** (`ledger.py:29` `self._events: list[ ]`), with `AuditEventORM:86-110` DB table unused. Needs `AuditDBLedger` that `append()` inserts via `AsyncSession` — label as prod gap (data loss on restart). |

---

## 10. Tool Policy + Risk + Execution (§§16H, 21, 29)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| R1 | Risk LOW/MEDIUM/HIGH deterministic (Section 21) + §16K four levels (`LLM-only → Privileged`) | ✅ | `execution-gateway/execution_gateway/risk.py:57-63` `HIGH_RISK_ACTIONS` + `MEDIUM_RISK_ACTIONS`; `249-343` `classify()` deterministic; `doc 21` mapping endorsed. `router.py:DOMAIN_POOLS` implements §16K security levels via worker pools. |
| R2 | §29 5-level DataClassification (PUBLIC/INTERNAL/CONFIDENTIAL/PII/SECRET) + content hook + HIGH egress (EXPORT/BULK/PII/SECRET+external) | ✅ | `risk.py:48-55` `DataClassification`; `98-147` `classify_content()` regex for SSN/RRN/phone/secret; `161-212` `is_high_egress()` + `224-246` `get_egress_classification()`. Integrated in `proxy.py:218-224` via `data_classification` param. |
| T1 | Tool discovery via MCP Registry; resource/action normalization | ✅ | `execution-gateway/execution_gateway/app.py:134-141` `GET /v1/tools` from `mcp_registry.list_tools_detailed()`; `144-188` normalizes via `normalize_resource/canonicalize_action`; `mcp_registry.py:228+` registry impl. |
| T2 | Privileged Tool Proxy — capability + risk + trace propagation + delegation binding | ✅ | `proxy.py:154-365` full proxy; `44-93` `_parse_agent_context_header`; `197-227` authz hook; `237-266` capability required for HIGH; `314-329` trace propagation. |
| T3 | MCP transport routing with mock fallback vs. `force_transport` strict | ✅ | `proxy.py:126-152` `_try_transport_call`; `268-310` routing logic; `_TOOL_TO_MOCK` map. |

---

## 11. Memory Governance & Admin (§27)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| MG1 | Namespace isolation: `PERSONAL=user/{id}` / `TEAM=group/{id}` / `CORPORATE=organization` + provenance + retention + classification | ✅ | `security/memory-governance/governance/governance.py:25-42` `MemoryScope/MemoryRecord` with `classification/retention_policy`; `100-113` `scope_namespace()`; `127-252` `MemoryStore.write()` with `source_resource_id/acl_version/delegation_id` indexes; `147-252` validation. |
| MG2 | Revoke cascade invalidation (`invalidate_by_delegation`) | ✅ | `governance.py:270+` `invalidate_by_delegation` iterates `_by_delegation` index, marks `invalidated/reason`. |
| MG3 | ORM persistence with pgvector (1536) + sqlite fallback + source tracking | ✅ | `security/models/orm.py:147-189` `MemoryORM` with `_VECTOR_1536 = pgvector.Vector(1536) else Text`; `172-188` `MemorySourceORM`. |

---

## 12. Cross-Cutting Gaps & Remediation Priority

### P0 — must fix before prod hardening review

| Gap | Remediation |
|---|---|
| **Approval/Audit in-memory only** (`ApprovalStore._requests dict`, `AuditLedger._events list`) — restart loses grant + chain | Wire `ApprovalStore` → `ApprovalRequestORM` + `AuditLedger` → `AuditEventORM` via `AsyncSession`; add alembic migration. |
| **Rate limiter not wired** (`ToolRateLimiter` instantiated nowhere) | Instantiate in `execution-gateway/execution_gateway/app.py` global and call `allow(f"{tenant}:{user}:{tool}")` before authz; return `429 + Retry-After` on deny. |
| **Control-plane skips RuntimeRouter** on session creation — `EXECUTE runtime/hermes` never evaluated | Insert `RuntimeRouter.select_runtime(user_id, task_type)` in `app.py:create_session` (or `mattermost_adapter/webhook.py:_handle_core_logic`) before `route_session`; propagate denial as `APPROVAL_REQUIRED`. |
| **Committed bot token** in `control-plane/.env:2` | Rotate token immediately; add `.env.example` with placeholders; add `git-secrets` pre-commit hook; set `OAOS_CP_MATTERMOST_BOT_TOKEN` via deployment secrets manager only. |
| **Workspace namespace not enforced** (16A.3) | Add `WorkspaceAllocator` that derives `/home/hermes/workspaces/{tenant}/{agent}/{session}` and passes to `ACPAdapter.create_session_remote`; add test `test_workspace_isolation.py` asserting no cross-session file access. |

### P1 — fix before v1.6 GA

| Gap | Remediation |
|---|---|
| Port 8100 bridge doc/code drift | Either alias container `mattermost-bridge:8100 → control-plane:8000` in compose, or update architecture doc to state `Mattermost webhook = 8000 /v1/mattermost/events` (actual). |
| `AdminStateORM` not wired | Implement `admin-console/backend/persistence.py` CRUD against `AdminStateORM`; front admin-console lib/api.ts to hit new `/v1/admin/state`. |
| Credential `refreshable/issued_at` columns missing | Add `issued_at`, `refreshable`, `last_used_at` update on `retrieve`; migrate ORM. |
| `nonce` replay only on `decide`, not on `verify` transport | Move `_seen_nonces` check into `verify()` so replayed `approval_id` fetch fails earlier. |
| Prod compose hardens ports correctly, **dev compose exposes DB/Redis publicly** | Add `profiles: ["debug"]` to `5432/6379` ports or bind `127.0.0.1` in `docker-compose.dev.yml`. |

### P2 — polish

| Item | Note |
|---|---|
| `hermes_model` default `qwen2.5` | Consistent with gateway; document model catalog in `deploy/.env.example`. |
| `session_store` global is `InMemorySessionStore` by default; Redis only if `OAOS_SESSION_BACKEND=redis` | Correct per spec (“Redis is hot, DB is truth”); add note in `control-plane/README.md` that prod must set `OAOS_SESSION_BACKEND=redis`. Keep `fallback=True` for zero-downtime upgrades. |
| Hermes `HOME` is `/home/hermes` but container `WORKDIR` is `/app` | Systemd path and container path diverge — choose one canonical (`/home/hermes`) for both, or document mapping. |

---

## 13. Task-Specific Verdicts (direct Q&A)

| Task Question | Answer | Pointer |
|---|---|---|
| **Hermes single-path?** | ✅ Yes. Canonical `InternalAgentInterface` (`internal_api.py`), single `ACPAdapter` (`acp_adapter.py`), single instance in `app.py:26` + `webhook.py:156,264`. No bypass routes. | `acp_adapter.py:1-13,25-28` |
| **Ollama removal?** | ✅ Removed. Zero `ollama` imports; fallback is Hermes Gateway `http://…:8642/v1/chat/completions` with `OAOS_CP_HERMES_API_KEY` Bearer (`acp_adapter.py:142-145`). Config declares `hermes_model` not ollama model. | `acp_adapter.py:12; config.py:9; .env:11` |
| **Mattermost bridge 8100?** | ⚠️ Partial — no `:8100` service; webhook lives at `control-plane:8000 /v1/mattermost/events`. Search for literal `8100` returns zero code hits. Needs compose alias or doc fix. | `deploy/docker-compose.prod.yml:170-193` ports; `grep 8100` empty |
| **Thread `root_id`?** | ✅ Implemented end-to-end. | `webhook.py:146,271,309; adapter.py:219-220,228-229` |

---

## 14. Positive Findings (do not regress)

- **Policy engine** is textbook §25: deterministic, `fnmatch`, no LLM, explicit-deny override proven (`engine.py:67` comment).
- **Vault** owner-check + Fernet + `PERSONAL_CREDENTIAL_USE` audit is §10-compliant; dual-mode DB/in-memory keeps tests green.
- **Capability token** replay+revoke+TTL is §26-complete; both stateful (`TokenService`) and stateless helper variants exist.
- **Risk classifier** (§21+§29) is unusually thorough — content hook regex + high-egress matrix + `PII/SECRET` escalation.
- **Execution proxy** correctly gates HIGH-risk with `CAPABILITY_REQUIRED` and propagates `trace_id/request_id/delegation_id`.
- **Mattermost approval card** (4 buttons) + `/slash` + `/actions` + HMAC covers §23 UX spec fully.

---

## 15. Files Created/Modified

- Created: `/home/openitsvc/open-agent-os/GAP_AUDIT_CORE_PLATFORM_v1.6.md` (this report).
- Modified: none (read-only audit).

## 16. Method

- Read `docs/architecture-v1.6.md` §§5-26 (90011 bytes, 4253 lines, offsets 1→3001).
- Enumerated `control-plane/control_plane/*.py` (app, config, session, identity, mattermost_adapter/webhook, acp_adapter, runtime_router, concurrency), `execution-gateway/execution_gateway/*.py` (app, authz_hook, capability, tool_policy, risk, proxy, mcp_registry, data_access, knowledge), `security/**` (policy-engine/engine+default_bundle, credential-vault/vault, models/orm+db, audit/ledger, delegation/service, token/service, approval/workflow, memory-governance/governance, app.py), `adapters/mattermost/adapter.py`, `packages/runtime-adapter/*`, `deploy/*`.
- Grepped for `ollama|8100|root_id|vault|capability|signature|hash.*chain|bulk` and for class/def surface.
- No process execution beyond reads/greps; no synthetic data.

