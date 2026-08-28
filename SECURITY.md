# Security Policy

## Supported Versions

| Version | Supported | Notes |
|---|---|---|
| `v1.5` | ✅ | Current canonical — Source-Available (BSL 1.1, Change Date 2030-08-27 → Apache 2.0), `docs/architecture-v1.5.md` (§§16A–16K) |
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

- `docs/architecture-v1.5.md` — 47 Sections + §§16A–16K (SHA `b19f54ab`, 3417 lines) — conformance: `docs/architecture-conformance.md` v1.5; Previous `v1.4.1` `646a8fe` / `v1.3` `4a0383c8` preserved; code: `packages/runtime-adapter` (LLM canonical) + `execution-gateway/tool_policy` + `data_access` (180 tests)
- `docs/security-model.md` — §§16F/16G/16H/16I reference

## Scope

**In scope:**
- `control-plane` — Identity (`derive_agent_id` 1:1), Session (`assert_owner` → 403), Router, ACP
- `execution-gateway` — MCP Registry, normalize, Risk (§21), Tool Policy (§16H — validate/rate-limit/bulk §16H.1–3), Data Access (§16I — read_only_api/command_api, direct DB DENY), authz_hook, Proxy (trace, HIGH token required), connectors (Google `check_owner` / Outline ACL)
- `security` — Runtime Registry/Router (§16F Dual Runtime, `llm` canonical/`safe` alias, 3 options, 5-step, `EXECUTE runtime/*`), Policy Engine (§25 Strict, `Explicit Deny > Personal`, `Agent Permission ≤ User Permission`), Delegation (fingerprint / cascade revoke), Vault (Fernet, `agent:assistant:<user>` isolation), Token (HS256 300s, nonce/jti replay), Approval (HMAC, 4 decisions), Audit (hash-chain + HMAC checkpoint)
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

- Architecture: [`docs/architecture-v1.5.md`](docs/architecture-v1.5.md) (§16A–16K, §§16F–16I, §30–31) — canonical v1.5
- Security Model: [`docs/security-model.md`](docs/security-model.md) (§§16A–16I, v1.5 — Dual Runtime / Untrusted Worker / Tool Policy / Data Access)
- Conformance: [`docs/architecture-conformance.md`](docs/architecture-conformance.md) v1.5 — 180 tests passed
- Threat review — Execution Gateway bypass: [`docs/security-review-gateway-bypass.md`](docs/security-review-gateway-bypass.md) — why "cannot bypass" matters more than "gateway exists", and the 3 remaining production hardenings (NetworkPolicy / Runtime hardening / DB re-verification)

A bypass of the Gateway via direct Hermes → DB / Internal API / credential access is considered a **security boundary failure**, not a feature.

## Hardening Guidance for Operators

Self-hosted operators should:

- Do not mount `docker.sock` into Hermes or Gateway containers
- Set `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, `seccomp` / `AppArmor`
- Apply Kubernetes `NetworkPolicy` + host `nft` / `systemd` — Hermes may only reach `control-plane:8000` / `execution-gateway:8001`, not DB / internal APIs directly — see `deploy/firewall/hermes-egress.nft` + `deploy/systemd/hermes.service` + `docs/security-model.md` (Blast Radius)
- Apply host firewall — `deploy/firewall/hermes-egress.nft` (nftables, §16A.6): `hermes` uid egress DENY by default, explicit DENY for INTERNAL_DB_NET/ERP/CRM/SSH, ALLOW only ACP:8000 / MCP:8001 / LLM — `sudo nft -f /etc/nftables/hermes-egress.nft`
- Apply systemd sandbox — `deploy/systemd/hermes.service` (§16A.4–16A.5): `User=hermes`, `NoNewPrivileges=true`, `ProtectSystem=strict`, `PrivateTmp=true`, `ReadWritePaths=/home/hermes` — `sudo systemd-analyze verify /etc/systemd/system/hermes.service`
- Keep `OAOS_SIGNING_KEY` / `FERNET_KEY` out of images and logs — use secrets management
- See `deploy/` for reference configurations

---

*Last updated: 2026-08-28 — v1.5 canonical (180 tests). For general questions (non-security), use GitHub Issues.*
