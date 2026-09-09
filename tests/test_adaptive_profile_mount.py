"""Adaptive Profile — app mount integration test (MVP v1.7.2).

Proves /v1/profile endpoints are exposed via control_plane.app.
Does NOT apply migration or start service; verifies router mount only.
"""

import os

UNIFIED_KEY = "test-unified-oaos-signing-key-32bytes-long-enough!!"
for _k in ("OAOS_SIGNING_KEY", "OAOS_USER_JWT_SIGNING_KEY", "OAOS_SECURITY_SERVICE_SIGNING_KEY", "OAOS_JWT_SIGNING_KEY", "ADMIN_JWT_SECRET"):
    os.environ[_k] = UNIFIED_KEY
os.environ.setdefault("OAOS_USER_JWT_ISSUER", "open-agent-os-auth")
os.environ.setdefault("OAOS_JWT_ISSUER", "open-agent-os-auth")
os.environ.setdefault("OAOS_USER_JWT_AUDIENCE", "control-plane")
os.environ.setdefault("OAOS_JWT_AUDIENCE", "control-plane")


def test_profile_router_mounted_in_app():
    """control_plane.app must expose /v1/profile/* routes (MVP, not live until migration)."""
    from control_plane.app import app

    def collect_paths(routes):
        paths = set()
        for route in routes:
            path = getattr(route, "path", None) or getattr(route, "path_format", "")
            if path:
                paths.add(path)
            nested = getattr(route, "routes", None)
            if nested:
                paths.update(collect_paths(nested))
            original = getattr(route, "original_router", None)
            if original is not None:
                paths.update(collect_paths(getattr(original, "routes", ())))
        return paths

    paths = collect_paths(app.routes)

    expected = {
        "/v1/profile/me",
        "/v1/profile/policy",
        "/v1/profile/preferences",
        "/v1/profile/evidence",
        "/v1/profile/reset",
    }
    missing = expected - paths
    assert not missing, f"profile routes not mounted in control_plane.app, missing={missing}, available={sorted(paths)}"


def test_profile_openapi_exposed():
    """OpenAPI schema must contain /v1/profile/me when router is mounted."""
    from control_plane.app import app

    schema = app.openapi()
    assert "/v1/profile/me" in schema["paths"], "openapi missing /v1/profile/me"
    assert "/v1/profile/policy" in schema["paths"]
    assert "/v1/profile/evidence" in schema["paths"]


def test_profile_unauth_returns_401():
    """Mounted routes should enforce auth (401 without token) — proves routing is live in process."""
    from control_plane.app import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        resp = c.get("/v1/profile/me")
        # No bearer -> 401 (router requires verified identity)
        assert resp.status_code == 401
        # Ensure not 404 (which would mean not mounted)
        assert resp.status_code != 404
