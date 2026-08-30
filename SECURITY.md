# Security Policy

## Supported Versions

| Version | Supported | Notes |
|---|---|---|
| `v1.7.2` | ✅ | Current canonical architecture design — includes Adaptive Profile Engine (§16.12), H4–H8 evidence tiers, ACL-aware Knowledge Index RAG, production hardening, Docker/systemd parallel deployment |
| `v1.7.1` | ✅ | Previous canonical baseline — H4–H8 evidence tiers, ACL-aware Knowledge Index RAG, H7 production mock immutable gate, Docker/systemd parallel deployment |
| `v1.6.4` | ✅ | Historical supported baseline — Source-Available (BSL 1.1, Change Date 2030-08-27 → Apache 2.0), quota/usage/HA |
| `v1.6.3` | ✅ | Previous — Source-Available (BSL 1.1, Change Date 2030-08-27 → Apache 2.0), `docs/architecture-v1.6.3.md` (§§16A–16K + §16.1.1–16.1.2 LLM 6-Provider + §27B Wiki Vault, 4732 lines, SHA `2868226b`) |
| `v1.6.2` | ✅ | Previous — Source-Available (BSL 1.1, Change Date 2030-08-27 → Apache 2.0), `docs/architecture-v1.6.2.md` (4526 lines, SHA `4456bd4c`) |
| `v1.5.1` | ✅ | Previous — Source-Available (BSL 1.1, Change Date 2030-08-27 → Apache 2.0), `docs/architecture-v1.5.1.md` (3615 lines, SHA `4c2c1b85`) |
| `v1.5` | ✅ | Previous — Source-Available (BSL 1.1, Change Date 2030-08-27 → Apache 2.0), `docs/architecture-v1.5.md` (3417 lines, SHA `b19f54ab`) |
| `v0.1.1` | ✅ | Previous — Source-Available (BSL 1.1, Change Date 2030-08-27 → Apache 2.0) |

Earlier pre-release tags are not supported. Security fixes are applied to `main`.

## Reporting a Vulnerability

**Do not open a public issue** for security vulnerabilities.

Use one of these private channels:

1. **GitHub — Private vulnerability reporting** (preferred)
   `Security` → `Report a vulnerability` on https://github.com/openit-ai/open-agent-os
2. **Email:** `mykim@openit.co.kr` (CC `apps@openit.co.kr`)
   Subject: `[O-AOS Security] <short title>`

You will receive an acknowledgement within **3 business days**.

### What to include

- Affected component / version / commit (`v1.5`, `v0.1.1`, `main@<sha>`)
- Reproduction steps or PoC (payload, request, log)
- Impact — what an attacker can achieve (cross-user access, token replay, bypass, etc.)
- Suggested fix if any

Please encrypt sensitive PoCs if needed — we will share a PGP key on request.

## Response & Disclosure

- **Triage:** within 3 business days
- **Fix target:** 90 days for coordinated disclosure (shorter for critical / actively exploited)
- We ask you to **keep the issue private** until a fix and advisory are published.
- We will credit reporters in the advisory unless you prefer to remain anonymous.
- Fixes are published as a GitHub Security Advisory (GHSA) and a patched release.

## Canonical Architecture

- [`docs/architecture-v1.7.2.md`](docs/architecture-v1.7.2.md) — current canonical implementation architecture; v1.7.2 Adaptive Profile Engine design is included in §16.12. Also covers ACL-aware Enterprise Knowledge Index RAG, Personal Wiki owner isolation, H4–H8 evidence tiers, production hardening, Secret lifecycle, and Docker/systemd parallel deployment.
- [`docs/architecture-v1.7.2-design.md`](docs/architecture-v1.7.2-design.md) — v1.7.1 design source, hardening contracts, and residual/live-integration boundaries.
- `docs/security-model.md` — security boundary reference

## Scope

**In scope:**
- `control-plane` — Identity (`derive_agent_id` 1:1), Session (`assert_owner` → 403), Router, ACP
- `execution-gateway` — MCP Registry, normalize, Risk (§21), Tool Policy (§16H — validate/rate-limit/bulk §16H.1–3), Data Access (§16I — read_only_api/command_api, direct DB DENY), authz_hook, Proxy (trace, HIGH token required), connectors (Google `check_owner` / Outline ACL)
- `security` — Runtime Registry/Router (§16F Dual Runtime, `llm` canonical/`safe` alias, 3 options, 5-step, `EXECUTE runtime/*`), Policy Engine (§25 Strict, `Explicit Deny > Personal`, `Agent Permission ≤ User Permission`), Delegation (fingerprint / cascade revoke), Vault (Fernet `OAOS_VAULT_KEY` sha256→b64 derive, `vault://admin_llm_providers/{id}/api_key`, `encrypted_api_key=gAAAAA…`, `****` masking, DB-backed + fail-soft fallback, `agent:assistant:<user>` isolation), Token (HS256 300s, nonce/jti replay), Approval (HMAC, 4 decisions), Audit (hash-chain + HMAC checkpoint), LLM Providers (6-Provider Registry: claude/codex/gemini/opencode-go/openrouter/ollama, `runtime_mode` conditional, `opencode` alias, opencode-go binary chain)
- `admin-console` — Auth (L5/L4, JWT 8h), RBAC enforcement
- `§16F Dual Runtime` — LLM Runtime (canonical, Safe alias) / Hermes Runtime, Registry YAML 3 options (LLM Only / Hermes Only / Both), Router 5-step, `EXECUTE runtime/*` Capability
- `§16G Untrusted Execution Worker` — Capability vs Authority 분리, Shell=Meta Capability, Blast Radius (`/home/hermes` 경계)
- `§16H Tool Policy` — `validate_tool_call` (allowed/denied fields, max_results), `ToolRateLimiter` (token-bucket), `is_bulk` (threshold 100)
- `§16I Data Access` — Read Replica / Read-only API → MCP, Command API + Approval, Direct DB DENY (`execution-gateway/execution_gateway/data_access.py`)
- Deployment — `deploy/docker-compose.*` / `deploy/k8s` NetworkPolicy / hardening when applicable

**Out of scope:**
- Denial of Service, rate-limit bypass without security impact
- Social engineering, physical access
- Vulnerabilities in third-party dependencies without a demonstrated exploit in O-AOS context (please still report — we track them)
- Self-hosted operator misconfiguration (exposed `*.env`, `docker.sock` mount) — we provide hardening guides instead

## Security Model (reference)

Open Agent OS is a **Self-Hosted Enterprise Personal Agent Platform** — `Personal Delegation (my resources, delegated by me) ↔ Enterprise Authorization (company resources — policy + JIT approval)`, `Cross-user always DENY`, `Auditable (hash-chain)`.

- Architecture: [`docs/architecture-v1.7.2.md`](docs/architecture-v1.7.2.md) — current v1.7.2 architecture design, including Adaptive Profile Engine (§16.12), ACL-aware Knowledge Index RAG, Personal Wiki, and production security contracts. Previous `v1.7.1` baseline and historical versions are preserved.
- Security Model: [`docs/security-model.md`](docs/security-model.md) (§§16A–16I — Dual Runtime / Untrusted Worker / Tool Policy / Data Access, v1.5.1: 16A.3.1 workspace isolation + 16A.6 Controlled Egress Proxy)
- Conformance: [`docs/architecture-conformance.md`](docs/architecture-conformance.md) v1.6.4 — 612 tests passed
- Threat review — Execution Gateway bypass: [`docs/security-review-gateway-bypass.md`](docs/security-review-gateway-bypass.md) — why "cannot bypass" matters more than "gateway exists", and the 3 remaining production hardenings (NetworkPolicy / Runtime hardening / DB re-verification)

A bypass of the Gateway via direct Hermes → DB / Internal API / credential access is considered a **security boundary failure**, not a feature.

## v1.7.2 Security Contracts

The v1.7.2 architecture adds the Adaptive Profile Engine as a personalization layer, not an authorization layer. User instructions, organization policy, authorization, approval, and audit remain authoritative. Profile data is tenant/user isolated, and Runtime receives only the minimum response policy required for the current task; detailed evidence and behavioral history are not exposed to the LLM. The v1.7.2 MVP is implemented in the confirmed scope — code, operational DB migration(014_adaptive_profile), CP router mount(`/v1/profile`), Mattermost ingress/ACP hook, image active-runtime E2E — with distributed/external/live RAG unverified (requires separate operational verification).

## v1.7.1 Security Contracts

### Secret lifecycle

- Canonical independent secrets: `JWT_SIGNING_KEY`, `AUDIT_SIGNING_KEY`, `ADMIN_JWT_SECRET`, `OAOS_ENCRYPTION_KEY`.
- `VAULT_ENCRYPTION_KEY` is a compatibility alias, not a separate user-entered secret.
- New systemd installs generate missing 64-hex secrets automatically; existing installs preserve strong values.
- Secret rotation occurs only with explicit `--rotate-secrets` and may invalidate JWT sessions and encrypted data access.
- Secrets remain in `EnvironmentFile` with mode `0600`; values must never appear in logs, CI artifacts, or GitHub.

### RAG security boundary

- Enterprise Knowledge Index is derived data; Outline/Notion/Drive remain source of truth for document ACLs.
- Tenant and source ACL filtering occurs before retrieval, not as a post-retrieval cleanup.
- ACL version changes and source deletion invalidate indexed chunks before they can be returned.
- Personal Wiki uses owner-isolated `tenant_id` + `agent_id` scope.
- Live external connector credentials, network access, and production corpus backfill require separate operational verification.

### Evidence boundary

- Unit tests do not count as distributed or external evidence.
- `scripts/verify-evidence-tiers.py` records the command, commit, timestamp, counts, and unavailable prerequisites.
- Current evidence must distinguish `unit`, `distributed`, and `external` tiers.

## Hardening Guidance for Operators

Self-hosted operators should:

- Do not mount `docker.sock` into Hermes or Gateway containers
- Set `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, `seccomp` / `AppArmor`
- Apply Kubernetes `NetworkPolicy` + host `nft` / `systemd` — Hermes may only reach `control-plane:8000` / `execution-gateway:8001`, not DB / internal APIs directly — see `deploy/firewall/hermes-egress.nft` + `deploy/systemd/hermes.service` + `docs/security-model.md` (Blast Radius)
- Apply host firewall — `deploy/firewall/hermes-egress.nft` (nftables, §16A.6 Controlled Egress Proxy): `hermes` uid egress DENY by default, explicit DENY for INTERNAL_DB_NET/ERP/CRM/SSH, ALLOW only ACP:8000 / MCP:8001 / LLM Gateway / Approved Package Mirror via Controlled Egress Proxy — `sudo nft -f /etc/nftables/hermes-egress.nft` — see `docs/architecture-v1.5.1.md` §16A.6
- Apply systemd sandbox — `deploy/systemd/hermes.service` (§16A.4–16A.5): `User=hermes`, `NoNewPrivileges=true`, `ProtectSystem=strict`, `PrivateTmp=true`, `ReadWritePaths=/home/hermes` — `sudo systemd-analyze verify /etc/systemd/system/hermes.service`
- Keep `OAOS_SIGNING_KEY` / `OAOS_VAULT_KEY` / `FERNET_KEY` out of images and logs — use secrets management (Vault: `OAOS_VAULT_KEY`/`VAULT_ENCRYPTION_KEY` → Fernet, `vault://` secret_ref, never plaintext in DB)
- See `deploy/` for reference configurations

---

*Last updated: 2026-08-30 — v1.7.2 MVP implemented (code·operational DB migration·CP router mount·Mattermost ingress/ACP hook·image active-runtime E2E confirmed; distributed/external/live RAG unverified) — see docs/architecture-v1.7.2.md §16.12. For general questions (non-security), use GitHub Issues.*
