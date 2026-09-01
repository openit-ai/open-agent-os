from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.error import HTTPError

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "oaos-mm-bridge.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("oaos_bridge_error_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_permanent_cp_errors_are_not_retryable():
    bridge = load_bridge()
    for status in (400, 401, 403, 404, 405, 409, 422):
        error = HTTPError("http://cp", status, "rejected", {}, None)
        assert bridge._is_permanent_cp_error(error) is True


def test_transient_cp_errors_are_retryable():
    bridge = load_bridge()
    for status in (408, 429, 500, 502, 503, 504):
        error = HTTPError("http://cp", status, "retry", {}, None)
        assert bridge._is_permanent_cp_error(error) is False
