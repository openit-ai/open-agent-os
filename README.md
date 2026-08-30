# Open Agent OS v0.1.1 — Personal AX Business Platform

> **Self-Hosted Enterprise Personal Agent OS** — One Personal Agent per Employee, bridging personal and enterprise work securely — Source-Available (BSL 1.1)

[한국어](README.ko.md) | **English**

<p align="center">
  <img src="assets/oaos-logo.jpg" alt="OAOS logo" width="220" />
</p>

<h1 align="center">OAOS</h1>
<p align="center"><strong>Where people, knowledge, and agents meet.</strong></p>

- **Brand:** OAOS
- **Repository:** `openit-ai/open-agent-os`
- **Canonical architecture:** [`docs/architecture-v1.7.0.md`](docs/architecture-v1.7.0.md) — 5026 lines, SHA `2ebeb981`

---

## 1. Product Definition

**Open Agent OS (OAOS)** is a **self-hosted Enterprise Personal Agent OS** that gives every employee **one Logical Personal Agent** — the employee's digital work identity — to connect personal work context and enterprise shared context securely.

- **Personal side (delegated by the employee):** Email, Calendar, Drive, Tasks, SaaS, Memory — connected via personal delegation without admin approval.
- **Enterprise side (governed by policy + JIT approval):** Mattermost / Slack, Outline / Notion, ERP / CRM / GitHub, internal systems — access is decided by the Policy Engine, never by the LLM.
- **Self-hosted:** Runs on the customer's server / VPS / private cloud / K8s. No multi-tenant SaaS lock-in, data stays on customer infrastructure.

OAOS does not replace Mattermost, Slack, Outline, Notion, or Hermes. It is the **Control + Security + Execution platform** that connects them around the Personal Agent.

**Why this structure exists — two contradictions (§2):**

1. *You cannot entrust personal data to a shared agent.* A shared `company-agent` cannot answer whose Gmail or Drive it is. Centralizing credentials creates exposure, cross-user leakage, and memory contamination.
2. *An agent that cannot reach personal tools has no reason to be used daily.* Without mail, calendar, documents, tasks, and memory, the agent stays an occasional search bot — low usage, low delegation, no AX transformation.

**OAOS answer:** `Personal Work Environment (Personal Delegation) ↔ Logical Personal Agent ↔ Enterprise Shared Environment (Policy + Approval)` — the agent becomes a daily executor: `discover → organize → search → coordinate → execute → request approval → integrate → remember` (§1, §14).

## 2. Personal Wiki

**Personal Wiki** is the per-employee private knowledge space bound to the Personal Agent. It works consistently across **Mattermost / Slack Web · Desktop · Mobile via ACP**, so work started on a phone continues naturally on a tablet or PC.

```text
Mattermost / Slack Web·Desktop·Mobile
                ↓ ACP
       Employee Personal Agent
                ↓
 Personal Wiki = Vault + Memory + Search
```

- Accumulates conversations, documents, meetings, schedules, and task outcomes over time.
- Structured by `tenant_id` / `agent_id` — each employee's context is isolated.
- Vault (file store) and Memory Service (pgvector + search) are linked via `source_ref` so the original, embedding, and provenance stay connected.
- Reuses accumulated knowledge for morning briefings, document discovery, coordination, and follow-up execution.

Implementation: `§27B Vault` — layout `/var/lib/oaos/vault/{tenant}/{agent:assistant:*}/{journal,notes,projects,files,attachments}`, attachment extraction (pdf/docx/xlsx/pptx/image OCR), automatic archiving of every tool result via Execution Gateway, and owner-isolated search via `memory_service` (pgvector 1536 + TF-IDF fallback). Details: [`docs/personal-wiki-design.md`](docs/personal-wiki-design.md).

## 3. Enterprise Knowledge Index & ACL-aware Hybrid RAG

Enterprise wikis and corporate documents are searched with **ACL-aware Hybrid RAG**. Outline, Notion, and other source systems keep the original documents and authoritative ACLs. OAOS stores a derived **Knowledge Index** in `oaos` PostgreSQL + pgvector for retrieval.

```text
Outline / Notion (originals + current ACLs)
        ↓ Connector / MCP — chunking, embedding, source reference, content hash, ACL version
 oaos PostgreSQL + pgvector
        ├─ lexical index
        ├─ semantic vector index
        ├─ tenant / group / agent metadata
        ├─ source reference (source_resource_id, source_uri, content_hash, source_updated_at)
        └─ ACL provenance (acl_version)
        ↓ verified Agent Context
 ACL pre-filter → lexical + semantic retrieval → deduplication & reranking → source reference → Personal Agent answer
```

**Knowledge Index fields:** `index_id, source_system, source_resource_id, source_uri, tenant_id, group_id/agent_id, chunk_id, chunk_text, embedding (pgvector 1536), content_hash, source_updated_at, indexed_at, acl_version, classification, retention_policy, provenance` — the index is a derived search accelerator; the source system remains the source of truth.

**Search order:**

1. Resolve `tenant_id` / user / group / agent scope from verified Agent Context.
2. Build candidate scope from source ACLs and `acl_version`.
3. Run lexical and semantic retrieval in parallel.
4. Deduplicate and rerank by metadata, recency, and document type.
5. Attach `source reference` and provenance to the answer.
6. On ACL or source change, refresh or revalidate the affected index entries.

**Critical invariant:** ACLs are applied **before** candidates are retrieved, not after results are ranked. Personal Wiki search is scoped by `agent_id`; enterprise search is scoped by source ACLs (user / group / collection) and `tenant_id`. Results always carry title, source system, URL, updated time, and provenance so the user can jump from search to the original and to execution.

**Continuity across clients:** The same Personal Agent is used from any client; a single question can target Personal Wiki only, the enterprise Knowledge Index only, or both.

### Adaptive Profile Engine — work the way your team works

OAOS v1.7.2 introduces the architecture for an **Adaptive Profile Engine**: instead of treating personalization as a static user record, OAOS learns how each employee prefers to collaborate with an agent and turns those signals into a focused response policy.

- **Personalized interaction, not personality labeling** — learn observable work preferences such as conclusion-first communication, evidence depth, decision speed, and confirmation style without exposing psychological types.
- **Context-aware by design** — combine global behavior with task-specific preferences for research, engineering, writing, meetings, and more.
- **Explicit control always wins** — the user’s current instruction overrides stored preferences; organization policy, authorization, and approval rules always override personalization.
- **Private by default** — profiles and evidence remain tenant/user isolated. Runtime receives only the minimum policy needed for the current task, never a detailed behavioral history.
- **Runtime-independent** — the design separates the Profile Engine from Runtime Adapters, enabling the same personalization layer across Hermes and future runtimes.
- **Quietly improving in the background** — post-interaction evidence processing is asynchronous, so personalization improves over time without slowing the critical response path.

> **The longer an agent works with your team, the better it understands not only what to remember, but how to work.**

> **Design status:** The Adaptive Profile Engine is an architecture feature in v1.7.2; the dedicated API, worker, Runtime Hook, and Profile Skills are planned for implementation and are not claimed as currently implemented.


```text
"what I handled last week"          → Personal Wiki + work tools
"company policy and related docs"   → Enterprise Knowledge Index
"compare my work with company policy" → Personal Wiki + Knowledge Index
```

## 4. Architecture Flow

```text
Mattermost / Slack ──► Control Plane (Identity / Session / ACP) ──► Hermes Runtime (Security-Domain Worker Pool)
                                  │  Internal Agent API                  │ Tool / MCP
                                  └──────► Execution Gateway (MCP Registry / Risk / AuthZ / Proxy) ──► Personal (Google) / Shared (Outline) / Enterprise (CRM/ERP)
                                                              ▲
                                              Security (Delegation / Vault / Policy / Token / Approval / Audit)
Admin Console (Next.js + shadcn) ──► Security API proxy ──────┘  users / policy / approvals / audit / credentials / infra

Personal Wiki (Vault FS + memory_service pgvector) ◄── Execution Gateway auto-archive (every tool call, trace_id)
Enterprise Knowledge Index (Postgres + pgvector) ◄── Connectors (Outline/Notion) — ACL versioned
```

**Invariants:** `Personal Delegation (my resources, delegated by me) ↔ Enterprise Authorization (company resources — policy + approval)`, `Explicit Deny > Personal`, `Agent Permission ≤ User Permission`, `Cross-user always DENY`, `Auditable (hash-chain + HMAC checkpoint)`.

**Runtime:** LLM Runtime canonical (`llm`, `safe` is deprecated alias) + Hermes Runtime advanced — Registry YAML (LLM Only / Hermes Only / Both), Router 5-step, Capability `EXECUTE runtime/*`, untrusted worker (§16G), tool policy (§16H), data access (§16I). See `docs/architecture-v1.7.0.md` §§16A–16K, §§16.1.1–16.1.2, §§16.4–16.8.

## 5. Core Values

1. **Personal-First, Enterprise-Safe** — Calendar / Gmail delegated by the employee (§9); Production / ERP / customer DB governed by company policy + approval (§11). Natural UX and safety together (§13).
2. **True isolation** — `agent:assistant:kim` sees only `employee:kim`-owned resources. Cross-user is always DENY, no plaintext token storage, no long-term credential storage in the Hermes process (§10).
3. **Human-approved high-risk execution** — HIGH-risk actions (`MERGE / DEPLOY / PAY / EXPORT`, §21) require a Capability Token (HS256, 300s, nonce/jti replay protection) + HMAC approval request (§24) + a 4-choice Admin Console decision (Deny / Once / Always for user / Always for group).
4. **Auditable operations** — Every authorization, delegation, and execution is recorded in the Audit Ledger as a hash-chain with HMAC checkpoint — tampering is immediately detectable (§30–31). `verify_chain` / `checkpoint` APIs.
5. **Self-Hosted, Source-Available** — BSL 1.1 (converts to Apache 2.0 after 4 years), deploy on customer infrastructure with installation, monitoring, backup, and support options (§5).

## 6. Quick Start

```bash
git clone https://github.com/openit-ai/open-agent-os.git && cd open-agent-os

# 1) Python 3.11 — tests
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"  # or pip install -r requirements.txt
pytest -q
```

```bash
# 2) Admin Console
cd admin-console && npm install && npm run build   # static routes
OAOS_ENV=development ADMIN_JWT_SECRET=dev-only-change-me npm run dev  # :3000 — bootstrap login via ADMIN console
  # Dev/test only (OAOS_ENV != production): seeded L5 admin via bootstrap.
  # Production: set OAOS_ADMIN_BOOTSTRAP_PASSWORD (never commit, never log) — app fails closed if missing.
```

```bash
# 3) Backends (separate terminals, from repo root)
uvicorn security.app:app --port 8001 --reload
uvicorn admin_console.backend.app:app --port 8002 --reload
uvicorn control_plane.app:app --port 8000 --reload
```

```bash
# 4) MVP demo (without Hermes)
curl -X POST http://localhost:8000/v1/demo/morning-briefing \
  -H "X-User-Id: employee:kim" -H "X-Tenant-Id: default" | jq
# → briefing (summary, today_meetings 4, emails 7, trace_id, audit_events 14)

curl -X POST http://localhost:8000/v1/mattermost/events \
  -H "Content-Type: application/json" \
  -d '{"text":"summarize","user_id":"employee:kim"}' | jq  # keyword routing
```

### Docker (evaluation — unchanged)

```bash
cp .env.example .env   # set OAOS_SIGNING_KEY etc. — Docker path preserved
docker compose -f deploy/docker-compose.dev.yml up -d
# services: control-plane(:8000) execution-gateway(:8001) admin-api(:8002) admin-console(:3000) postgres redis
```

### Systemd (production, non-Docker — new)

```bash
# 1) Env template (unified, systemd-only — Docker's .env is untouched)
cp config/oaos.env.example config/oaos.env
chmod 600 config/oaos.env
# 신규 설치: 누락 Secret은 installer가 64-hex로 자동 생성
# 기존 설치: 기존 Secret 보존, 교체는 명시적으로 --rotate-secrets 사용

# 2) Friendly preflight (no secret output, shows file:line for missing vars)
bash scripts/check-production-config.sh --env-file config/oaos.env

# 3) Install (user unit mirrors 192.168.6.61 :8100 without sudo; system unit needs sudo)
bash deploy/systemd/install-systemd.sh --user --env-file config/oaos.env --dry-run  # preview
bash deploy/systemd/install-systemd.sh --user --env-file config/oaos.env
systemctl --user status oaos-control-plane.service
curl -s http://127.0.0.1:8100/healthz | jq

# System-wide (requires sudo, uses /etc/oaos/oaos.env):
sudo mkdir -p /etc/oaos && sudo cp config/oaos.env.example /etc/oaos/oaos.env
sudo chmod 600 /etc/oaos/oaos.env && sudo vi /etc/oaos/oaos.env
bash scripts/check-production-config.sh --env-file /etc/oaos/oaos.env
sudo bash deploy/systemd/install-systemd.sh --env-file /etc/oaos/oaos.env
# With optional execution-gateway (8001) + security (8002) — only if entrypoints verify:
sudo bash deploy/systemd/install-systemd.sh --env-file /etc/oaos/oaos.env --with-optional
```
See [`deploy/systemd/README.md`](deploy/systemd/README.md) and [`config/oaos.env.example`](config/oaos.env.example).

## 7. Deployment

| Edition | Includes |
|---|---|
| Developer | Source access, base Personal Agent, local evaluation |
| Business | + Vault / JIT Approval / Audit / Admin Console / IAM |
| Managed | Business + installation / monitoring / backup on customer-owned infra |

Self-hosted on customer server / VPS / private cloud / K8s — not multi-tenant SaaS.

**Choose your runtime — Docker vs systemd vs K8s (same code, separate config):**

| Aspect | Docker | systemd (non-Docker) | K8s |
|---|---|---|---|
| **Config** | `.env` + `deploy/docker-compose.*.yml` (preserved) | `config/oaos.env.example` → `config/oaos.env` or `/etc/oaos/oaos.env` | `deploy/k8s/configmap.yaml` + `secret.yaml.template` |
| **Preflight** | `docker compose config` | `bash scripts/check-production-config.sh --env-file <path>` (no secret output, file:line errors) | `kubectl apply --dry-run` + same check script |
| **Install** | `docker compose -f deploy/docker-compose.prod.yml up -d` | `bash deploy/systemd/install-systemd.sh [--user] --env-file <path>` | `kubectl apply -f deploy/k8s/` |
| **Units** | containers (postgres, redis, control-plane, execution-gateway, security, nginx) | `oaos-control-plane.service` :8100 (primary) + optional `oaos-execution-gateway.service` :8001, `oaos-security.service` :8002 | Deployments `control-plane`, `execution-gateway`, `security` |
| **Verify** | `docker compose ps` / `curl https://localhost/healthz` | `systemctl [--user] status oaos-control-plane` / `curl http://127.0.0.1:8100/healthz` / `journalctl -u oaos-control-plane` | `kubectl get pods` / `kubectl exec ... curl /healthz` |
| **Isolation** | `oaos-net` bridge, `expose` (no host ports in prod) | systemd hardening (`NoNewPrivileges`, `ProtectSystem`, `PrivateTmp`) + `ReadWritePaths` | `NetworkPolicy` + `PodSecurity` |
| **Secret handling** | `env_file: ../.env` + `:? required` | `EnvironmentFile=/etc/oaos/oaos.env` (0600, never printed) — 신규 설치 누락 Secret 자동 생성, 기존 설치 보존, `--rotate-secrets` 명시 시에만 교체 | `Secret` + `ConfigMap` |

**Compose & K8s (unchanged):**

- `deploy/docker-compose.dev.yml` — evaluation; `deploy/docker-compose.prod.yml` — healthcheck (`curl -f /healthz`, interval 30s) + `restart: unless-stopped` + `depends_on: service_healthy` for nginx gating.
- `deploy/k8s/` — `replicas: 2`, `RollingUpdate(maxUnavailable:1,maxSurge:1)`, `livenessProbe` on `/healthz` (30s), `readinessProbe` on `/readyz` (10s), `podAntiAffinity(hostname)`, `PodDisruptionBudget(minAvailable:1)`, `HPA` (2–10, CPU 70% / mem 80%). See [`docs/ha.md`](docs/ha.md) and [`docs/deployment.md`](docs/deployment.md).
- **systemd:** `deploy/systemd/oaos-control-plane.service` (system) + `deploy/systemd/user/oaos-control-plane.service` (user, mirrors 192.168.6.61 :8100) + optional `oaos-execution-gateway.service`/`oaos-security.service` (verified entrypoints only). Env template `config/oaos.env.example` (also at `deploy/systemd/oaos.env.example`). See [`deploy/systemd/README.md`](deploy/systemd/README.md).

- **Health endpoints (all runtimes):** `GET /healthz` (liveness), `GET /readyz` (readiness, bounded DB/Redis checks), `GET /v1/health/detailed` (detailed latency). In production, failing dependencies cause `/readyz` to return `503` so the pod/unit is removed from traffic.

Next on the roadmap: single KVM4 VPS integrating Mattermost + Outline + Hermes + OAOS via `deploy/docker-compose.prod.yml` (Docker) or `deploy/systemd/*` (systemd).

## 8. Security Boundaries

- **Policy Engine (§25):** fnmatch glob, strict evaluation, Explicit Deny overrides Personal Delegation.
- **Delegation:** Fingerprint + binding cascade revoke, immediate effect.
- **Vault:** Fernet AES+HMAC (`OAOS_VAULT_KEY` / `VAULT_ENCRYPTION_KEY` sha256→b64 derive, `vault://admin_llm_providers/{id}/api_key`, `encrypted_api_key=gAAAAA…`, `****` masking), owner `agent:assistant:<user>` isolation, `EncryptedPostgresVault` DB-backed + fail-soft in-memory fallback.
- **Token:** HS256 300s short-lived + nonce/jti replay store.
- **Approval:** HMAC-SHA256, 4 decisions (`DENIED / APPROVED_ONCE / APPROVED_USER_ALWAYS / APPROVED_GROUP_ALWAYS`), nonce/signature/expiry.
- **Audit:** Hash-chain + HMAC checkpoint (`verify_chain`, `checkpoint`).
- **Isolation guarantees:** `agent:assistant:kim` cannot access `employee:lee` resources. Verified by `test_delegation_isolation`, `test_cross_user_session_isolation 403`, `test_app_policy_evaluate_explicit_deny`, `test_audit_verify_chain+tamper`.
- **Network & worker isolation (§§16A, 16G–16I):** Hermes runs as untrusted worker (`hermes` uid, `/home/hermes` sandbox, `nftables` + Controlled Egress Proxy — only ACP:8000, MCP:8001, LLM Gateway, approved package mirror are allowed; direct access to production DB, ERP/CRM, SSH, other user homes, and vault secrets is denied). See [`docs/security-model.md`](docs/security-model.md) and `deploy/firewall/hermes-egress.nft`.

Admin Console screens (11+ routes: login / dashboard / infra / users / policy / approvals / audit / credentials / providers) are shadcn + WCAG AA, `overflow-auto` for 375px, `npm run build` verified. Infra screen probes `GET /v1/infra/health` in parallel (3s timeout, 15s polling); write requires L5, read requires L4.

## 9. Verification Evidence

Run locally at any commit — no external claims required:

```bash
pytest -q
# Expected (2026-08-29, main): 927 passed, 1 skipped, 74 warnings — includes LLM 6-Provider, Fernet Vault,
# opencode binary chain, wiki/pgvector, and production hardening (fail-closed runtime/deploy/audit/approval/token/rate + secrets).
python scripts/verify-evidence-tiers.py --check-only  # H8: fails if docs claim unsupported distributed/external
# Filtered examples:
pytest tests/test_workstream_a.py tests/test_control_plane_api.py -v  # isolation / SSE
pytest tests/test_workstream_b.py -v       # cross-user deny / HIGH token / trace
pytest tests/test_workstream_c.py -v       # policy / audit / vault / approval
pytest tests/test_mvp_demo.py -v           # kim vs lee isolation, EXPORT deny
pytest tests/test_admin_backend.py -v      # register / login / JWT / bcrypt / RBAC / health mock
```

| Tier | Count (2026-08-29) | Prerequisites | Evidence |
|------|-------------------|---------------|----------|
| unit | 927 passed, 1 skipped | none (local) | `pytest -q` — fakeredis/SQLite/file mocks allowed, no live infra |
| distributed | 0 passed | Redis + kind + K8s + CNI | requires `kind` + `redis-cli ping PONG` + `hubble --verdict DROPPED` |
| external | 0 passed | Outline/Notion/Mattermost/Slack/LLM gateway | requires live credentials + network |
| total | 927 passed |  | unit only; distributed/external remain 0 until live verification |

> `distributed`/`external` are not claimed from unit tests. See `docs/deployment-verification-v1.7.1.md` and `docs/evidence-report-v1.7.1.json` generated by `scripts/verify-evidence-tiers.py` (records command, timestamp, commit, counts, unavailable prerequisites).

- **This README reports the current measured result** (`pytest -q` on the checked-out commit). Do not treat it as a fixed guarantee — rerun to confirm after changes.
- **Liveness / readiness (fail-closed in production):** `GET /healthz` always `200`; `GET /readyz` returns `503` when DB/Redis checks fail in production (`non-prod` may return `200 degraded` with `checks` detail), and during `SIGTERM` draining (`terminationGracePeriodSeconds: 30`). K8s `readinessProbe` removes the pod from traffic on `503`.
- **Audit chain:** `verify_chain` detects tampering; `checkpoint` is HMAC-signed.
- **RAG distinction:** Knowledge Index (schema/repository/retrieval/chunking/embedding/Outline-Notion adapters/sync/ACL revalidation) is implemented and unit-tested; live external connector credentials/network and production corpus backfill remain operational integration work — not claimed as external evidence.

> **Historical note (for reference only):** `docs/architecture-v1.7.0.md` (`2ebeb981`, 5026 lines) was recorded at `648 tests` including production hardening (fail-closed runtime/deploy/audit/approval/token/rate + secrets). At `v1.6.4` the count was `612`; earlier milestones were `590` / `180`. Those numbers are point-in-time snapshots, not the current result.

## 10. Repository Structure

```text
packages/                  # Phase 0 contracts (Pydantic, immutable): agent-context, policy-model, audit-model, delegation-model, mcp-resource-model, common-types
control-plane/             # Identity (derive_agent_id 1:1), Session (assert_owner→403), Router (HIGH→ephemeral), ACP (X-Agent-Context, SSE), Internal API (§17), Demo
execution-gateway/         # normalize (domain/scope/path), mcp_registry (wildcard reverse-index), connectors (google check_owner / outline ACL), risk (§21), authz_hook, proxy (trace, HIGH requires token), mock_executor, wiki_archive hook
security/                  # policy-engine (§25), delegation (fingerprint + cascade revoke), vault (Fernet), token (HS256 300s + nonce/jti), approval (HMAC), audit (hash-chain + checkpoint), app (FastAPI)
admin-console/             # Next.js 15 + shadcn Financial (#22C55E/#F59E0B/#DC2626), 375px, routes: login/dashboard/infra/users/policy/approvals/audit/credentials/providers + backend (auth L5/L4 JWT, infra CRUD, health probe, policy/approval/audit/credentials proxy)
adapters/                  # Mattermost / Slack / Outline / Notion / Hermes / IAM / Google / Microsoft
packages/personal-wiki/    # Personal Wiki Vault FS (journal/notes/projects/files/attachments), extractor, consolidate, memory_service client
examples/morning-briefing/ # MVP — orchestrator (per-user kim vs lee) + output.json (13KB) + README
config/                    # oaos.env.example — systemd unified env template (Docker's .env preserved)
deploy/                    # docker-compose.dev/prod.yml + k8s (Section 32) + systemd (oaos-*.service) + firewall (hermes-egress.nft)
scripts/                   # check-production-config.sh — friendly preflight (no secret output)
tests/                     # see Verification Evidence — run pytest -q for the current count
docs/architecture-v1.7.0.md  # Canonical (47 Sections + §§16A–16K + §16.1.1–16.1.2 LLM 6-Provider + §§16.4–16.6 Quota/Usage/HA + §27B Wiki Vault + §§16.7–16.8 Production Hardening — SHA 2ebeb981)
```

## 11. Docs

- [`docs/architecture-v1.7.0.md`](docs/architecture-v1.7.0.md) — Canonical (47 Sections + §§16A–16K + §16.1.1–16.1.2 LLM 6-Provider + §§16.4–16.6 Quota/Usage/HA + §27B Wiki Vault + §§16.7–16.8 Production Hardening). Previous: [`docs/architecture-v1.6.4.md`](docs/architecture-v1.6.4.md) `e10c1af8` (historical), `docs/architecture-v1.1.md` preserved.
- [`docs/architecture-v1.7.1-design.md`](docs/architecture-v1.7.1-design.md) — Critical/High hardening design (C1/H1–H8, Personal Wiki JWT, Enterprise Knowledge Index spec, readiness strict, distributed state).
- [`docs/personal-wiki-design.md`](docs/personal-wiki-design.md) — Personal Wiki Vault / extractor / consolidation / memory_service integration.
- [`docs/security-model.md`](docs/security-model.md) — Dual runtime, untrusted worker, tool policy, data access, egress allowlist.
- [`docs/ha.md`](docs/ha.md) + [`docs/deployment.md`](docs/deployment.md) — HA probes, PDB, HPA, zero-downtime procedures.
- `docs/api/` — Internal Agent Interface, Capability, Approval APIs.
- `examples/morning-briefing/README.md` — MVP briefing format (`09:30 / 11:00 / Must-do today`).

## 12. License

**Business Source License 1.1** — see [`LICENSE`](./LICENSE).

- **Licensor:** OpenIT Co., Ltd. / **Licensed Work:** Open Agent OS
- **Additional Use Grant:** Non-production use for Developer Edition evaluation / development / testing; production / hosting / redistribution requires separate Business/Managed commercial license
- **Change Date:** `2030-08-27` (for v0.1.1; each later version 4 years after its release) — **Change License:** Apache 2.0
- BSL text: https://mariadb.com/bsl11/ · Apache 2.0: https://www.apache.org/licenses/LICENSE-2.0
