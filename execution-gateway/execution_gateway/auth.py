"""EGW auth shim — re-exports canonical signed_context to remove module ambiguity.

Canonical implementation lives in execution_gateway.signed_context.
This module is kept for backwards compatibility; do not add duplicate logic here.
"""
from .signed_context import (  # noqa: F401
    _DEV_SIGNING_KEY,
    ISSUER,
    AUDIENCE,
    get_signing_key,
    get_issuer,
    get_audience,
    _is_production,
    _allow_plaintext,
    _fail_open_telemetry,
    issue_agent_context_jwt,
    verify_agent_context_jwt,
    parse_and_verify_context,
)
