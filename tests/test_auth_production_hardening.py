"""Regression: admin-console auth production fail-closed hardening."""
from __future__ import annotations

import os
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"


def _load_auth_fresh(env_overrides: dict, clear_env_keys: list[str] | None = None):
    orig = {k: os.environ.get(k) for k in set(list(env_overrides.keys()) + (clear_env_keys or []))}
    present = {k: k in os.environ for k in orig}
    for k, v in env_overrides.items():
        os.environ[k] = v
    for k in (clear_env_keys or []):
        if k not in env_overrides:
            os.environ.pop(k, None)
    for mod in list(sys.modules.keys()):
        if mod in ("admin_auth_hardening", "auth", "admin_auth"):
            del sys.modules[mod]
    added = False
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
        added = True
    try:
        spec = importlib.util.spec_from_file_location("admin_auth_hardening", str(BACKEND / "auth.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["admin_auth_hardening"] = mod
        sys.modules["auth"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, orig_v in orig.items():
            if present[k]:
                os.environ[k] = orig_v if orig_v is not None else ""
                if orig_v is None:
                    os.environ.pop(k, None)
            else:
                os.environ.pop(k, None)
        if added and str(BACKEND) in sys.path:
            sys.path.remove(str(BACKEND))
        for m in ("admin_auth_hardening", "auth"):
            sys.modules.pop(m, None)


def test_production_no_bootstrap_fails_closed():
    with_blocks = False
    try:
        _load_auth_fresh(
            {"OAOS_ENV": "production", "ADMIN_JWT_SECRET": "strong-prod-secret-32-bytes-min-xyz!"},
            clear_env_keys=["OAOS_ADMIN_BOOTSTRAP_PASSWORD", "OAOS_ADMIN_BOOTSTRAP_TOKEN", "OAOS_DATABASE_URL", "DATABASE_URL"],
        )
    except RuntimeError as e:
        assert "OAOS_ADMIN_BOOTSTRAP_PASSWORD" in str(e) or "fail-closed" in str(e)
        with_blocks = True
    assert with_blocks, "production without bootstrap must fail-closed"


def test_production_with_bootstrap_succeeds_and_not_default_password():
    mod = _load_auth_fresh(
        {
            "OAOS_ENV": "production",
            "ADMIN_JWT_SECRET": "strong-prod-secret-32-bytes-min-xyz!2",
            "OAOS_ADMIN_BOOTSTRAP_PASSWORD": "StrongBootstrap!1234",
            "OAOS_ADMIN_BOOTSTRAP_EMAIL": "admin@openit.co.kr",
        },
        clear_env_keys=["OAOS_DATABASE_URL", "DATABASE_URL"],
    )
    user = mod.get_user_by_email("admin@openit.co.kr")
    assert user is not None
    assert user.role.value == "L5"
    assert mod._verify_password("StrongBootstrap!1234", user.hashed_password) is True
    assert mod._verify_password("Admin123!", user.hashed_password) is False
    assert mod.get_user_by_email("admin@openit.co.kr") is None or mod.get_user_by_email("admin@openit.co.kr").email != "admin@openit.co.kr" or len(mod._users_by_email) == 1
    assert user.hashed_password != "StrongBootstrap!1234"


def test_production_dev_jwt_rejected():
    try:
        _load_auth_fresh({"OAOS_ENV": "production"}, clear_env_keys=["ADMIN_JWT_SECRET"])
        assert False, "should have raised for dev JWT default"
    except RuntimeError as e:
        assert "ADMIN_JWT_SECRET" in str(e)


def test_production_prod_alias_also_rejected():
    try:
        _load_auth_fresh({"OAOS_ENV": "prod"}, clear_env_keys=["ADMIN_JWT_SECRET"])
        assert False, "should have raised for dev JWT with prod alias"
    except RuntimeError as e:
        assert "ADMIN_JWT_SECRET" in str(e)


def test_dev_still_seeds_default():
    mod = _load_auth_fresh(
        {"OAOS_ENV": "development", "ADMIN_JWT_SECRET": "dev-admin-jwt-secret-please-change"},
        clear_env_keys=["OAOS_ADMIN_BOOTSTRAP_PASSWORD", "OAOS_ADMIN_BOOTSTRAP_TOKEN", "OAOS_DATABASE_URL", "DATABASE_URL"],
    )
    user = mod.get_user_by_email("admin@openit.co.kr")
    assert user is not None
    assert mod._verify_password("Admin123!", user.hashed_password) is True


def test_prod_compose_requires_OAOS_ENV():
    prod = (ROOT / "deploy" / "docker-compose.prod.yml").read_text()
    assert "OAOS_ENV" in prod
    assert "${OAOS_ENV:?" in prod


def test_k8s_configmap_has_OAOS_ENV_production():
    cm = (ROOT / "deploy" / "k8s" / "configmap.yaml").read_text()
    assert "OAOS_ENV" in cm
    assert "production" in cm


def test_k8s_deployments_require_OAOS_ENV():
    for f in ["deploy/k8s/control-plane/deployment.yaml", "deploy/k8s/security/deployment.yaml", "deploy/k8s/execution-gateway/deployment.yaml"]:
        txt = (ROOT / f).read_text()
        assert "OAOS_ENV" in txt, f"{f} must reference OAOS_ENV"


def test_no_hardcoded_secret_literals_in_tracked_configs():
    for readme in [ROOT / "README.md", ROOT / "README.ko.md"]:
        txt = readme.read_text()
        assert "Admin123!" not in txt, f"{readme.name} must not contain hardcoded Admin123!"
    env_example = (ROOT / ".env.example").read_text()
    assert "openagentos:secret@" not in env_example
    assert "oaos:secret@" not in env_example
    auth_txt = (BACKEND / "auth.py").read_text()
    assert "fail-closed" in auth_txt.lower()
    assert "OAOS_ADMIN_BOOTSTRAP_PASSWORD" in auth_txt
