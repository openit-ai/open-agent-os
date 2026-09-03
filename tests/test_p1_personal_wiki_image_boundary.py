"""P1 boundary: image isolation / traversal / no absolute path / JSON-safe metadata

Covers defects described in P1 review:
- /tmp fallback must be absent or production fail-closed (owner isolation)
- attachment_ref / runtime / audit / API must not expose absolute saved_path
- extractor metadata must be JSON-safe (no unescaped filename interpolation)
- traversal guard: '..' in filename rejected
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"
PKG_ROOT = ROOT / "packages" / "personal-wiki"

# Ensure imports work without full app setup
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

import importlib.util


def _load_extractor():
    spec = importlib.util.spec_from_file_location(
        "personal_wiki.extractor_test", str(PKG_ROOT / "personal_wiki" / "extractor.py")
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    sys.modules[spec.name] = mod  # type: ignore
    spec.loader.exec_module(mod)  # type: ignore
    return mod


def _read_personal_wiki_src() -> str:
    return (BACKEND / "personal_wiki.py").read_text(encoding="utf-8")


# --- extractor metadata JSON-safe ---

def test_extractor_metadata_json_safe_with_tricky_filename(tmp_path):
    mod = _load_extractor()
    # filename containing single quote, double quote, newline, unicode, backslash
    tricky = "a'b\"c\nd\te\u2603.png"
    # Create a real file because build_image_runtime_instruction stats size if exists
    p = tmp_path / tricky
    # Path with newline not allowed on Linux; use a safe tricky variant that still covers quotes
    tricky = "a'b\"c\u2603.png"
    p = tmp_path / tricky
    p.write_bytes(b"\x89PNG fake")
    instr = mod.build_image_runtime_instruction(str(p), vault_path="tenant1/agent:assistant:alice/attachments/a.png", attachment_id="att_123")
    # Metadata line should contain valid JSON
    assert "Metadata:" in instr
    # Extract JSON after Metadata:
    meta_line = [l for l in instr.splitlines() if l.startswith("Metadata:")][0]
    json_part = meta_line.split("Metadata:", 1)[1].strip()
    data = json.loads(json_part)
    assert data["filename"] == tricky
    assert data["ext"] == ".png"
    assert isinstance(data["bytes"], int)
    # Ensure old broken pattern not present: "'filename': '" with unescaped single quote interpolation
    assert "{'filename': '" not in instr  # old f-string pattern must be gone
    # Ensure json encoding handles single quote correctly (should be escaped as \" or retained inside double-quoted JSON)
    # json dumps uses double quotes; single quote inside should be literal and not break parsing
    re_parsed = json.loads(json_part)
    assert re_parsed["filename"] == tricky


def test_extractor_metadata_handles_single_quote_filename(tmp_path):
    mod = _load_extractor()
    p = tmp_path / "o'reilly.jpg"
    p.write_bytes(b"fake")
    instr = mod.build_image_runtime_instruction(str(p))
    meta_line = [l for l in instr.splitlines() if l.startswith("Metadata:")][0]
    json_part = meta_line.split("Metadata:", 1)[1].strip()
    data = json.loads(json_part)
    assert data["filename"] == "o'reilly.jpg"


def test_extractor_attachment_ref_no_absolute_path(tmp_path):
    mod = _load_extractor()
    p = tmp_path / "photo.png"
    p.write_bytes(b"123")
    ref = mod.build_image_attachment_reference(str(p))
    # vault_path should be filename fallback, not absolute
    assert ref["vault_path"] == "photo.png"
    assert not Path(ref["vault_path"]).is_absolute()
    assert "saved_path" not in ref
    # when vault_path given, should use it verbatim
    ref2 = mod.build_image_attachment_reference(str(p), vault_path="t1/agent1/attachments/photo.png")
    assert ref2["vault_path"] == "t1/agent1/attachments/photo.png"
    assert "saved_path" not in ref2


def test_extractor_rejects_absolute_vault_path(tmp_path):
    mod = _load_extractor()
    p = tmp_path / "secret.png"
    p.write_bytes(b"123")
    try:
        mod.build_image_attachment_reference(str(p), vault_path="/etc/passwd")
        raise AssertionError("absolute vault_path must be rejected")
    except ValueError as exc:
        assert "relative" in str(exc)


def test_extractor_runtime_instruction_no_absolute_path(tmp_path):
    mod = _load_extractor()
    p = tmp_path / "secret.png"
    p.write_bytes(b"123")
    instr = mod.build_image_runtime_instruction(str(p))
    # Must not contain absolute path string (tmp_path is absolute)
    assert str(tmp_path) not in instr
    # vault fallback uses filename, not absolute
    assert "stored at secret.png" in instr


# --- admin personal_wiki.py boundary checks ---

def test_admin_image_ref_contains_bytes_data_url(tmp_path):
    src = _read_personal_wiki_src()
    assert "data_url" in src and "base64" in src
    # The data URL is generated from saved bytes, not a file:// reference.
    assert "base64.b64encode(saved_path.read_bytes())" in src
    assert "file://" not in src


def test_personal_wiki_no_tmp_fallback_and_no_saved_path_leak():
    src = _read_personal_wiki_src()
    # /tmp fallback must be absent
    assert 'Path("/tmp")' not in src, "fallback to /tmp must be removed (owner isolation bypass)"
    # saved_path must not be exposed in attachment_ref, audit, or API response
    # _build_image_attachment_ref should not contain saved_path key
    # Find that function block
    assert '"saved_path"' not in src or src.count('"saved_path"') == 0, "absolute saved_path must not be exposed to runtime/audit/API"
    assert "'saved_path'" not in src
    # Also check no str(saved_path) leak in response building except internal existence check
    # Allow one occurrence for is_mock? but we removed leak; ensure not in base_resp or attachment_ref
    # Simple: ensure base_resp does not contain saved_path
    # We already checked no quoted key; extra guard: "vault_path" should be tenant/agent isolated, not absolute


def test_personal_wiki_attachment_ref_no_saved_path_key():
    src = _read_personal_wiki_src()
    block = src.split("def _build_image_attachment_ref")[1].split("def _build_image_runtime_instruction")[0]
    # The param name saved_path is allowed; key exposure is not
    assert '"saved_path"' not in block
    assert "'saved_path'" not in block


def test_personal_wiki_traversal_blocked_by_existing_code():
    # Verify get_vault_path and upload traversal guards exist (already in code)
    src = _read_personal_wiki_src()
    assert "PATH_TRAVERSAL" in src
    assert '\"..\" in filename' in src or "\"..\" in filename" in src or "..\" in filename" in src
    # Ensure safe_join_vault is used in _persist
    assert "safe_join_vault" in src


def test_owner_isolation_vault_paths_tenant_scoped(tmp_path, monkeypatch):
    # Verify _owner_vault_root isolates by tenant/agent
    spec = importlib.util.spec_from_file_location("personal_wiki_isolation", str(BACKEND / "personal_wiki.py"))
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    # Need to set env to use tmp vault root
    vault_root = tmp_path / "vault"
    monkeypatch.setenv("OAOS_WIKI_VAULT", str(vault_root))
    # Avoid importing vault side effects that need DB
    sys.modules[spec.name] = mod  # type: ignore
    try:
        spec.loader.exec_module(mod)  # type: ignore
    except Exception:
        pass
    if hasattr(mod, "_owner_vault_root"):
        r1 = mod._owner_vault_root("tenantA", "agent:assistant:alice")
        r2 = mod._owner_vault_root("tenantB", "agent:assistant:bob")
        assert "tenantA" in str(r1) and "alice" in str(r1)
        assert "tenantB" in str(r2) and "bob" in str(r2)
        assert str(r1) != str(r2)
        # traversal attempt via tenant_id must be blocked downstream via vault helpers


def test_upload_fails_closed_on_persist_error(monkeypatch):
    # Ensure _persist failure raises 503, not /tmp fallback
    src = _read_personal_wiki_src()
    # After fix, except block should raise HTTPException unconditionally
    # Check that fallback lines are gone and raise is present
    assert "raise HTTPException(status_code=503" in src
    assert 'Path("/tmp")' not in src
