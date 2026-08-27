# Security Model — Personal Delegation vs Enterprise Authorization (Sections 8-13)

## 16G. Hermes Security Model: Untrusted Execution Worker (v1.4.1)

Open Agent OS는 Hermes의 정상 동작을 전제로 하지 않는다. Hermes는 trusted security component가 아니라 **potentially compromised / untrusted execution worker**로 취급한다.

### 16G.1 Capability vs Authority 분리

- Hermes Capability: Reasoning / Planning / Shell / Python / Local Files / Skills / Task Orchestration / MCP
- Enterprise Authority: Production Credential / ERP Admin Token / DB Password / SSH Private Key / Broad LAN Access / Security Core Secret

> 원칙: Agent Capability와 Enterprise Authority를 분리한다.

```
Hermes                   = 문제 해결 능력
Security Core/MCP Gateway = 실제 기업 권한
```

### 16G.2 Shell = Arbitrary Tool Generator (Meta Capability)

Shell은 일반 Tool과 동일하게 취급하지 않는다. Shell은 `curl`, `wget`, `python`, `node`, `nc`, `ssh`, `psql`, `mysql`, custom TCP client, compiled binary 등 새로운 도구를 즉석에서 만들 수 있다.

```
Shell ≈ Arbitrary Tool Generator
```

그러나 Shell 자체를 제거하는 것이 목표는 아니다.

```
Shell Allowed
+ Filesystem Restricted
+ Network Restricted
+ Credential Absent
+ Enterprise Access via MCP
```

Hermes는 `/home/hermes` 안에서 자유롭게 코드를 생성하고 실행할 수 있지만, 그 코드가 기업 시스템에 직접 연결될 수 있는 capability는 갖지 않는다.

### 16G.3 Blast Radius Principle

```
Hermes Compromised
        ↓
Can modify /home/hermes
Can execute local code
Can consume assigned runtime resource
Can request MCP tools
        ↓
Cannot directly access:
- Production DB
- ERP / CRM
- SSH
- Other user home
- Credential Vault
- Security Core secret
```

보안 목표는 Hermes가 항상 올바르게 행동하도록 만드는 것이 아니라, 잘못 행동해도 기업 자원을 직접 파괴할 수 없도록 하는 것이다.

**Blast Radius 다이어그램:**

```
┌─────────────────────────────────────────────────┐
│            Hermes Runtime (Untrusted)           │
│  ┌───────────────────────────────────────────┐  │
│  │  /home/hermes  (Allowed)                  │  │
│  │  - local code execution                   │  │
│  │  - local file modify                      │  │
│  │  - assigned CPU/mem                       │  │
│  └───────────────────────────────────────────┘  │
│                     │                           │
│          MCP / Execution Gateway                │
│          (Capability + Policy + Audit)          │
│                     │                           │
│   ─────── Blast Radius Boundary ─────────────── │
│                     │                           │
│   DENY: Production DB / ERP / CRM / SSH         │
│   DENY: Other user home / Credential Vault      │
│   DENY: Security Core secret / Broad LAN        │
│                                                 │
│   ALLOW (via Gateway only):                     │
│   → Read:  Read Replica / Read-only API → MCP   │
│   → Write: Command API + Policy + Approval      │
└─────────────────────────────────────────────────┘
```

코드 레벨 주석: `packages/runtime-adapter/runtime_adapter/security_notes.py` 및 `execution-gateway/execution_gateway/data_access.py` 참조.

---

## 16I. Enterprise Data Access Pattern

기업 데이터 접근은 직접 DB access보다 정형 API/Gateway를 우선한다.

### 16I.1 Read Path

```
Production DB
    ↓
Read Replica / Read-only View
    ↓
Query Service
    ↓
MCP Tool
    ↓
Agent
```

원칙: Read-only first, Least data / Least field / Least row. `allowed_read_sources = ["read_replica","read_only_api","mcp","query_service"]`

### 16I.2 Write Path

```
Agent Request → MCP Tool → Security Core → Policy Engine → Risk Evaluation → Human Approval → Command API → Enterprise System
```

`allowed_write_sources = ["command_api","security_core","approval","mcp"]`, 모든 write는 `command_api + approval` 필요.

### 16I.3 Direct DB Access

```
Agent Runtime → Production DB = DENY
```

원칙적으로 Agent Runtime의 Production DB 직접접속을 금지한다. 필요한 접근은 `MCP → Query Service → Read-only API`를 우선한다. 코드: `DataAccessPolicy.direct_db_access(user, resource)`는 항상 DENY 반환.

---
