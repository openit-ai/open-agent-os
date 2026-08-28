# Security Model — Personal Delegation vs Enterprise Authorization (§§16A–16I, v1.5)

## 16F. Dual Runtime — LLM Runtime / Hermes Runtime (v1.5, alias §16E.6)

Open Agent OS는 모든 Personal Agent를 단일 고자율 Runtime으로 실행하지 않는다. 기본은 **LLM Runtime**(`llm` canonical — `safe`는 deprecated alias로 유지, 하위호환)으로 통제성과 예측성을 확보하고, 고복잡·고자율 작업에만 **Hermes Runtime**을 사용한다. Registry는 YAML `runtimes.llm`/`runtimes.hermes`의 `installed/enabled/security_level` 3옵션(LLM Only / Hermes Only / Both)을, Router는 5-step(Installed → Enabled → Capability `EXECUTE runtime/*` → Task suitability → Resource)으로 `llm`/`hermes`를 선택한다. Hermes 미설치 시에도 Personal Delegation / Policy / Vault / Audit 등 핵심 기능은 LLM Runtime만으로 정상 동작한다. Capability `EXECUTE runtime/*`는 JIT 부여 가능. Blast Radius(16G) 경계와 연계되어 Hermes가 compromised 되어도 Production 자원에 직접 도달하지 못하도록 한다. — 코드: `packages/runtime-adapter/runtime_adapter/{registry,router,safe_adapter,hermes_adapter} + security_notes.py`

---

## 16G. Hermes Security Model: Untrusted Execution Worker (v1.5)

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

## 16H. Execution Gateway — Tool Policy (§16H, v1.5)

Capability만으로는 위험한 argument/대량 호출을 막지 못한다. Tool Policy는 `validate_tool_call` — allowed/denied fields, `max_results` 등) + per-tenant/user/tool token-bucket rate limit(`ToolRateLimiter`) + bulk protection(`is_bulk` threshold 100, BULK_* HIGH escalation)을 결정론적으로 적용한다. LLM이 아닌 코드로 강제되며, `execution-gateway/proxy`와 `risk`에서 HIGH로 승격된다. — 코드: `execution-gateway/execution_gateway/tool_policy.py`

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

## References

- Canonical: `docs/architecture-v1.5.md` (3417 lines, SHA `b19f54ab`) — §§16F/16G/16H/16I, §§16A–16E carryover — §§16A–16I zero-trust boundary
- Conformance: `docs/architecture-conformance.md` v1.5 (180 tests, SHA `b19f54ab`) — Previous `v1.4.1` `646a8fe` / `v1.3` `4a0383c8` preserved
- Code: `packages/runtime-adapter/runtime_adapter/security_notes.py` (§16F/§16G Blast Radius), `packages/runtime-adapter/runtime_adapter/{registry,router,safe_adapter}`, `execution-gateway/execution_gateway/{tool_policy,data_access,proxy,risk}`, `security/*`
