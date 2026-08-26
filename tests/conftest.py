import sys
from pathlib import Path
# Ensure control-plane + packages are importable without manual PYTHONPATH (Workstream A+B+C)
ROOT = Path(__file__).resolve().parents[1]
for p in [
    ROOT / "control-plane",
    ROOT / "execution-gateway",
    ROOT / "security/policy-engine",
    ROOT / "security/delegation",
    ROOT / "security/credential-vault",
    ROOT / "security/crypto",
    ROOT / "security/audit",
    ROOT / "security/approval",
    ROOT / "security/memory-governance",
    ROOT / "security/token",
    ROOT / "packages/common-types",
    ROOT / "packages/agent-context",
    ROOT / "packages/policy-model",
    ROOT / "packages/audit-model",
    ROOT / "packages/delegation-model",
    ROOT / "packages/mcp-resource-model",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
# security/token collides with stdlib 'token' — need parent 'security' on path for legacy `import token.token_service`
if str(ROOT / "security") not in sys.path:
    sys.path.insert(0, str(ROOT / "security"))

import pytest
@pytest.fixture
def tenant_id(): return "test-tenant"

