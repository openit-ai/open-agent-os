import sys
from pathlib import Path
# Ensure control-plane + packages are importable without manual PYTHONPATH (Workstream A)
ROOT = Path(__file__).resolve().parents[1]
for p in [ROOT / "control-plane", ROOT / "packages/common-types", ROOT / "packages/agent-context", ROOT / "packages/policy-model", ROOT / "packages/audit-model", ROOT / "packages/delegation-model", ROOT / "packages/mcp-resource-model"]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest
@pytest.fixture
def tenant_id(): return "test-tenant"

