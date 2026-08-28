"""§16G/16A.3.1 Untrusted Execution Worker — Security Notes

16A.3.1 Session/User Workspace Isolation (v1.5.1):
  Namespace: /home/hermes/workspaces/{tenant}/{agent}/{session}/
  Path-name-only != isolation: path separation alone is NOT security isolation.
  Levels: general→per-session workspace+process isolation, sensitive→ephemeral sandbox, high-risk→ephemeral container/VM
  Retention: delete or safe-retain, no reuse; Cross-session deny: A cannot read B

§16G Untrusted Execution Worker — Security Notes

이 모듈은 v1.4.1 §16G/§16I 보안 모델의 코드 레벨 주석 역할을 한다.
Hermes를 trusted가 아닌 potentially compromised worker로 취급한다.

16G.1 Capability vs Authority 분리
---------------------------------
- Hermes Capability: Reasoning / Planning / Shell / Python / Local Files / Skills / MCP
- Enterprise Authority: Production Credential / DB Password / ERP Token / SSH Key / Vault Secret / Broad LAN
- 원칙: Agent Capability와 Enterprise Authority 분리
  Hermes = 문제 해결 능력
  Security Core / MCP Gateway = 실제 기업 권한
- Hermes가 Shell로 생성한 어떤 코드도 Enterprise 권한을 직접 보유하지 않는다.
  반드시 Execution Gateway → Capability Enforcement → Policy → Approval → Command API 경로를 거친다.

16G.2 Shell = Arbitrary Tool Generator (Meta Capability)
--------------------------------------------------------
- Shell은 일반 Tool이 아니라 Arbitrary Tool Generator로 취급한다.
  curl, wget, python, node, nc, ssh, psql, mysql, custom TCP client, compiled binary 등
  새로운 도구를 즉석에서 만들 수 있는 meta capability이다.
- Shell 자체를 제거하지 않는다. 대신 경계로 제한한다:
  Shell Allowed + Filesystem Restricted + Network Restricted + Credential Absent + Enterprise Access via MCP
- /home/hermes 내에서 자유롭게 코드 생성/실행을 허용하되,
  그 코드가 기업 시스템에 직접 연결될 수 있는 capability는 제거한다.

16G.3 Blast Radius Principle
---------------------------
목표: Hermes가 잘못 행동해도 기업 자원을 직접 파괴할 수 없도록 한다.
보안 목표는 올바른 행동 유도가 아니라, 오작동 시 피해 범위 제한이다.

Blast Radius 다이어그램 (주석):

    ┌─────────────────────────────────────────────────┐
    │            Hermes Runtime (Untrusted)           │
    │  ┌───────────────────────────────────────────┐  │
    │  │  /home/hermes  (Allowed)                  │  │
    │  │  - local code execution                   │  │
    │  │  - local file modify                      │  │
    │  │  - assigned CPU/mem                       │  │
    │  │  - MCP tool request (via Gateway)         │  │
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

Compromised 시 영향:
  Can:    /home/hermes 수정, local code 실행, 할당 자원 소모, MCP 요청
  Cannot: Production DB 직접 접근, ERP/CRM 직접 호출, SSH, 타 사용자 홈, Vault, Security Core secret

16I Data Access Pattern 연계
----------------------------
- Read Path:  Production DB → Read Replica / Read-only View → Query Service → MCP → Agent
  (원칙: Read-only first, Least data/field/row)
- Write Path: Agent → MCP → Security Core → Policy → Risk → Approval → Command API → Enterprise
  (고위험: DELETE/PAY/DEPLOY/MERGE 등)
- Direct DB: Agent Runtime → Production DB = DENY

이 주석은 docs/security-model.md의 코드 레벨 반영이며, gateway의 data_access.py와 함께 동작한다.
"""

# Re-export blast radius check for convenience (optional — execution-gateway may not be on PYTHONPATH outside gateway)
try:
    from execution_gateway.data_access import DataAccessPolicy, get_data_access_policy  # type: ignore
except Exception:
    DataAccessPolicy = None  # type: ignore
    get_data_access_policy = None  # type: ignore

__all__ = ["DataAccessPolicy", "get_data_access_policy", "WORKSPACE_ROOT", "WORKSPACE_ROOT_STR", "ISOLATION_LEVELS", "IsolationLevel"]

# ── 16A.3.1 Workspace Isolation constants (v1.5.1) ─────────────────────
from pathlib import Path as _Path

WORKSPACE_ROOT: _Path | str = _Path("/home/hermes/workspaces")
WORKSPACE_ROOT_STR: str = "/home/hermes/workspaces"

try:
    from .workspace import IsolationLevel as _IsolationLevel, ISOLATION_LEVELS as _ISOLATION_LEVELS  # type: ignore

    IsolationLevel = _IsolationLevel  # type: ignore
    ISOLATION_LEVELS = _ISOLATION_LEVELS  # type: ignore
except Exception:
    from enum import Enum as _Enum  # fallback

    class IsolationLevel(_Enum):  # type: ignore
        GENERAL = "general"
        SENSITIVE = "sensitive"
        HIGH_RISK = "high_risk"

    ISOLATION_LEVELS = {  # type: ignore
        "general": "per-session workspace + process isolation",
        "sensitive": "ephemeral sandbox",
        "high_risk": "ephemeral container or VM",
    }

# ── 16G 주석 상수 (문서/검증용) ──────────────────────────────────────

SHELL_IS_META_CAPABILITY = True  # Shell ≈ Arbitrary Tool Generator
CAPABILITY_VS_AUTHORITY_SEPARATED = True  # Hermes Capability ≠ Enterprise Authority

BLAST_RADIUS_ALLOWED = [
    "/home/hermes",
    "local_code_execution",
    "assigned_runtime_resource",
    "mcp_tool_request_via_gateway",
]

BLAST_RADIUS_DENIED = [
    "production_db",
    "erp",
    "crm",
    "ssh",
    "other_user_home",
    "credential_vault",
    "security_core_secret",
    "broad_lan_access",
    "direct_production_api",
]
