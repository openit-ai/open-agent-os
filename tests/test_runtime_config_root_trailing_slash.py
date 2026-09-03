"""Regression: runtime-config root GET must serve published DB snapshot for both
trailing-slash and non-trailing-slash paths, via query ?tenant_id= and header
X-Tenant-Id, preserving auth and signature checks. Covers the nginx /api -> 8010
case where frontend fetches /v1/runtime/config/?tenant_id=default vs
/v1/runtime/config?tenant_id=default."""

from __future__ import annotations
import importlib.util, sys, pathlib, os

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    import types
    for pkg in ("admin_console", "admin_console.backend"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg); m.__path__ = []
            sys.modules[pkg] = m
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod

os.environ.pop("OAOS_ENV", None)
os.environ["OAOS_CORS_ORIGINS"] = "http://localhost:3012"

from fastapi.testclient import TestClient

def _admin_client():
    auth = _load("admin_console.backend.auth", BACKEND / "auth.py")
    app_mod = _load("admin_console.backend.app", BACKEND / "app.py")
    return app_mod.app, auth

def _login(client, email="admin@openit.co.kr", password="Admin123!"):
    r = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}

def test_runtime_config_root_trailing_slash_and_query_vs_header():
    admin_app, _ = _admin_client()
    try:
        import admin_console.backend.runtime_config as rc
        rc.clear_runtime_config_state()
    except Exception:
        import importlib
        rc = importlib.import_module("admin_console.backend.runtime_config")
        rc.clear_runtime_config_state()

    ac = TestClient(admin_app, follow_redirects=False)
    hdr = _login(ac)

    # Ensure empty published returns 404 OR 200 if DB already has published from prior run
    # (clear may not fully purge persistent postgres on this host; we just verify both paths don't 500)
    for path in ["/v1/runtime/config/?tenant_id=default", "/v1/runtime/config?tenant_id=default"]:
        r = ac.get(path, headers=hdr)
        assert r.status_code in (200, 404, 307), f"{path} unexpected {r.status_code} {r.text}"
        if r.status_code == 404:
            assert "NOT_PUBLISHED" in r.text

    # Create and publish a fresh snapshot so root must return DB snapshot via both path variants
    # Use a unique tenant to avoid cross-run version drift on persistent DB
    import time
    tenant = f"test-root-{int(time.time()*1000)%1000000}"
    r = ac.post("/v1/runtime/config/snapshot", json={"tenant_id": tenant}, headers=hdr)
    assert r.status_code == 201, r.text
    v1 = r.json()["version"]
    assert v1 == 1, f"new tenant should start at v1, got {v1}"
    r = ac.post("/v1/runtime/config/snapshot", json={"tenant_id": tenant}, headers=hdr)
    assert r.status_code == 201, r.text
    v2 = r.json()["version"]
    r = ac.post("/v1/runtime/config/publish", json={"tenant_id": tenant, "version": v2}, headers=hdr)
    assert r.status_code == 200, r.text

    # With published DB snapshot, both trailing-slash paths must return 200 with version v2
    for path in [f"/v1/runtime/config/?tenant_id={tenant}", f"/v1/runtime/config?tenant_id={tenant}", "/v1/runtime/config/", "/v1/runtime/config"]:
        # for bare / and / without query, need tenant via header or defaults to default tenant; use header for tenant-specific check
        use_path = path if "tenant_id" in path else path
        headers = hdr if "tenant_id" in path else {**hdr, "X-Tenant-Id": tenant}
        # if path already has query, header not needed; for bare paths, add header
        r = ac.get(use_path, headers=headers if "tenant_id" not in path else hdr)
        # For bare paths without query and without header tenant, default tenant would differ, so skip bare path check for non-default tenant
        if "tenant_id" not in path:
            # Bare path with header should return tenant's published version
            r = ac.get(path, headers={**hdr, "X-Tenant-Id": tenant})
            assert r.status_code == 200, f"{path} with header {tenant} expected 200 got {r.status_code} {r.text} location={r.headers.get('location')}"
            assert r.json()["version"] == v2
            continue
        assert r.status_code == 200, f"{path} expected 200 got {r.status_code} {r.text} location={r.headers.get('location')}"
        body = r.json()
        assert body["version"] == v2
        assert body["tenant_id"] == tenant
        assert len(body.get("signature", "")) == 64

    # Header vs query interop: query tenant_id and X-Tenant-Id header should both resolve
    r_q = ac.get(f"/v1/runtime/config/?tenant_id={tenant}", headers=hdr)
    r_h = ac.get("/v1/runtime/config/", headers={**hdr, "X-Tenant-Id": tenant})
    assert r_q.status_code == 200 and r_h.status_code == 200
    assert r_q.json()["version"] == r_h.json()["version"] == v2

    # Also direct /v1/runtime/config?tenant_id= without slash must now be 200 not 307
    r = ac.get(f"/v1/runtime/config?tenant_id={tenant}", headers=hdr)
    assert r.status_code == 200, f"non-slash query should be 200 not 307, got {r.status_code} {r.headers.get('location')} {r.text[:200]}"

    # snapshots/status still work (DB primary)
    r = ac.get(f"/v1/runtime/config/snapshots?tenant_id={tenant}", headers=hdr)
    assert r.status_code == 200 and r.json()["count"] == 2
    r = ac.get(f"/v1/runtime/config/status?tenant_id={tenant}", headers=hdr)
    assert r.status_code == 200 and r.json()["published_version"] == v2
