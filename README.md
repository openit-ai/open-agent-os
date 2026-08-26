# Open Agent OS

> **설치형 Enterprise Personal Agent Platform** — 직원별 Logical Personal Agent를 중심으로 개인 업무환경과 기업 공동 업무환경을 안전하게 연결하는 Source-Available Platform

- **Repository:** `openit-ai/open-agent-os`
- **Architecture:** v1.1 — Agent Control Plane / Execution Gateway / Security & Governance 3분할
- **Deployment:** 고객사 서버 / 전용 VPS / Private Cloud / K8s (멀티테넌트 SaaS 아님)

## Architecture Summary

```
Human Workspace (Mattermost/Slack)
        ↓
Agent Control Plane (Identity, Session, ACP Adapter)
        ↓  Internal Agent API
Hermes Runtime (Security Domain Worker Pools)
        ↓  Tool/MCP
Agent Execution Gateway (MCP Registry, Capability Enforcement)
        ↓
Personal Tools (Gmail/Calendar/Drive/Tasks) | Shared Knowledge (Outline/Notion) | Enterprise (ERP/CRM/GitHub)

Security & Governance (Personal Delegation, Credential Vault, Policy Engine, JIT Approval, Audit Ledger)
```

핵심 원칙: **Personal Delegation (내 자원은 내가 위임) ↔ Enterprise Authorization (회사 자원은 정책+승인)**

## Repository Structure

```
control-plane/       # Agent Control Plane
execution-gateway/   # Agent Execution Gateway
security/            # Policy, Approval, Token, Audit, Delegation, Vault, Crypto
admin-console/       # Admin UI (shadcn)
adapters/            # Mattermost/Slack/Outline/Notion/Hermes/IAM/Google/Microsoft
packages/            # Shared domain models (agent-context, policy-model 등)
deploy/              # Docker, K8s, systemd
docs/                # Architecture & API docs
examples/            # Usage examples
tests/               # E2E & security tests
```

## Quick Start (Developer / Evaluation)

```bash
cp .env.example .env
docker compose -f deploy/docker-compose.dev.yml up -d
pnpm --filter admin-console dev   # optional
pytest tests/security/ -v
```

## Editions

| Edition | Description |
|---------|-------------|
| Developer | source access, basic Personal Agent, local evaluation |
| Business  | + Vault, JIT Approval, Audit, Admin Console, IAM |
| Managed   | Business + 고객 소유 인프라에 설치/모니터링/백업 |

## Security Invariants (must hold)

- `Agent Permission <= User Permission`
- Personal credential은 Hermes process에 장기 저장 금지, encrypted vault만
- Cross-user credential/memory 접근은 항상 DENY
- Explicit Deny는 Personal Delegation을 override
- 모든 권한 판단은 Policy Engine (LLM 아님), 감사 이벤트는 hash-chain

## Docs

- `docs/architecture-v1.1.md` — 최종 아키텍처 기준서 (본 레포의 truth)
- `docs/api/` — Internal Agent Interface, Capability, Approval API
- `docs/security-model.md` — Personal Delegation vs Enterprise Authorization

## License

Source-Available — Business/Managed는 별도 라이선스. Developer는 평가 목적.
