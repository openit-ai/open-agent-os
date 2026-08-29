import sys
import os
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
    ROOT / "packages/runtime-adapter",
    ROOT / "packages/personal-wiki",
    ROOT / "packages/agent-runtime",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
# security/token collides with stdlib 'token' — need parent 'security' on path for legacy `import token.token_service`
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "security") not in sys.path:
    sys.path.insert(0, str(ROOT / "security"))

# === Unified test signing key contract (C1/H1/H2/H3) ===
# One explicit env-configured key used by all services and test fixtures.
# Tests MUST generate tokens with the exact env-configured verification key,
# issuer/audience and tenant claims; do not hardcode production secrets.
UNIFIED_TEST_KEY = "test-unified-oaos-signing-key-32bytes-long-enough!!"
# Set all signing-key env vars that implementations check (priority order) to unified value
# This prevents cross-test env pollution when pytest collects all modules in one process.
for _k in (
    "OAOS_SIGNING_KEY",
    "OAOS_SECURITY_SERVICE_SIGNING_KEY",
    "OAOS_USER_JWT_SIGNING_KEY",
    "OAOS_JWT_SIGNING_KEY",
    "OAOS_AGENT_CONTEXT_SIGNING_KEY",
    "OAOS_AGENT_JWT_SIGNING_KEY",
    "OAOS_SIGNED_CONTEXT_SIGNING_KEY",
    "JWT_SIGNING_KEY",
    "ADMIN_JWT_SECRET",
    "OAOS_WIKI_JWT_SIGNING_KEY",
):
    os.environ[_k] = UNIFIED_TEST_KEY

# Unified issuer/audience (env-configured, matching verifier defaults)
# Control Plane user JWT
os.environ.setdefault("OAOS_USER_JWT_ISSUER", "open-agent-os-auth")
os.environ.setdefault("OAOS_JWT_ISSUER", "open-agent-os-auth")
os.environ.setdefault("OAOS_AUTH_ISSUER", "open-agent-os-auth")
os.environ.setdefault("OAOS_USER_JWT_AUDIENCE", "control-plane")
os.environ.setdefault("OAOS_JWT_AUDIENCE", "control-plane")
os.environ.setdefault("OAOS_AUTH_AUDIENCE", "control-plane")
# Agent context
os.environ.setdefault("OAOS_AGENT_CONTEXT_ISSUER", "control-plane")
os.environ.setdefault("OAOS_SIGNED_CONTEXT_ISSUER", "control-plane")
os.environ.setdefault("OAOS_AGENT_JWT_ISSUER", "control-plane")
os.environ.setdefault("OAOS_AGENT_CONTEXT_AUDIENCE", "execution-gateway")
os.environ.setdefault("OAOS_SIGNED_CONTEXT_AUDIENCE", "execution-gateway")
os.environ.setdefault("OAOS_AGENT_JWT_AUDIENCE", "execution-gateway")
# Security
# (security uses hard-coded ALLOWED_ISSUERS/AUDIENCE, not env; no need)

# Ensure non-prod for plaintext fallback tests (fail-closed only in production)
os.environ.pop("OAOS_ENV", None)

import pytest
@pytest.fixture
def tenant_id(): return "test-tenant"
