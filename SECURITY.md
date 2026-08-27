# Security Policy

## Supported Versions

| Version | Supported | Notes |
|---|---|---|
| `v0.1.1` | ✅ | Current — Source-Available (BSL 1.1, Change Date 2030-08-27 → Apache 2.0) |

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

- Affected component / version / commit (`v0.1.1`, `main@<sha>`)
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

## Scope

**In scope:**

- `control-plane` — Identity (`derive_agent_id` 1:1), Session (`assert_owner` → 403), Router, ACP
- `execution-gateway` — MCP Registry, normalize, Risk (§21), authz_hook, Proxy (trace, HIGH token required), connectors (Google `check_owner` / Outline ACL)
- `security` — Policy Engine (§25 Strict, `Explicit Deny > Personal`, `Agent Permission ≤ User Permission`), Delegation (fingerprint / cascade revoke), Vault (Fernet, `agent:assistant:<user>` isolation), Token (HS256 300s, nonce/jti replay), Approval (HMAC, 4 decisions), Audit (hash-chain + HMAC checkpoint)
- `admin-console` — Auth (L5/L4, JWT 8h), RBAC enforcement
- Deployment — `deploy/docker-compose.*` / `deploy/k8s` NetworkPolicy / hardening when applicable

**Out of scope:**

- Denial of Service, rate-limit bypass without security impact
- Social engineering, physical access
- Vulnerabilities in third-party dependencies without a demonstrated exploit in O-AOS context (please still report — we track them)
- Self-hosted operator misconfiguration (exposed `*.env`, `docker.sock` mount) — we provide hardening guides instead

## Security Model (reference)

Open Agent OS is a **Self-Hosted Enterprise Personal Agent Platform** — `Personal Delegation (my resources, delegated by me) ↔ Enterprise Authorization (company resources — policy + JIT approval)`, `Cross-user always DENY`, `Auditable (hash-chain)`.

- Architecture: [`docs/architecture-v1.1.md`](docs/architecture-v1.1.md) (§6, §16, §19–25, §30–31)
- Threat review — Execution Gateway bypass: [`docs/security-review-gateway-bypass.md`](docs/security-review-gateway-bypass.md) — why "cannot bypass" matters more than "gateway exists", and the 3 remaining production hardenings (NetworkPolicy / Runtime hardening / DB re-verification)

A bypass of the Gateway via direct Hermes → DB / Internal API / credential access is considered a **security boundary failure**, not a feature.

## Hardening Guidance for Operators

Self-hosted operators should:

- Do not mount `docker.sock` into Hermes or Gateway containers
- Set `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, `seccomp` / `AppArmor`
- Apply Kubernetes `NetworkPolicy` — Hermes may only reach `control-plane:8000` / `execution-gateway:8001`, not DB / internal APIs directly
- Keep `OAOS_SIGNING_KEY` / `FERNET_KEY` out of images and logs — use secrets management
- See `deploy/` for reference configurations

---

*Last updated: 2026-08-27 — v0.1.1. For general questions (non-security), use GitHub Issues.*
