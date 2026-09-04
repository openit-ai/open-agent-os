"""CP-side bounded extraction + owner-safe Vault read path.

Covers: owner/path validation (cross-owner, absolute, traversal refused),
extracted text bounded + secret-masked, sensitive skip, non-extractable
metadata-only, image preservation, and webhook/ACP handler import/compile.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CP = REPO / "control-plane"
if str(CP) not in sys.path:
    sys.path.insert(0, str(CP))

from control_plane.mattermost_adapter.attachment_extract import (  # noqa: E402
    MAX_EXTRACTED_CHARS,
    enrich_attachment_refs,
    mask_secrets,
)

TENANT = "default"
AGENT = "agent:assistant:alice"


@pytest.fixture()
def vault_root(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    monkeypatch.setenv("OAOS_WIKI_VAULT", str(root))
    owner = root / TENANT / AGENT / "attachments" / "fid1"
    owner.mkdir(parents=True, exist_ok=True)
    return root


def _ref(**kw):
    base = {
        "file_id": "fid1",
        "attachment_id": "fid1",
        "kind": "stored_only",
        "filename": "report.txt",
        "mime_type": "text/plain",
        "size": 10,
        "source": "mattermost",
        "vault_path": f"{TENANT}/{AGENT}/attachments/fid1/report.txt",
        "stored": True,
        "sha256": "abc",
        "extractable": True,
        "extract_hint": "txt",
        "extracted_text": None,
    }
    base.update(kw)
    return base


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# owner / path validation
# ---------------------------------------------------------------------------

def test_cross_owner_ref_refused(vault_root):
    other = vault_root / "other" / AGENT / "attachments" / "fid1"
    other.mkdir(parents=True, exist_ok=True)
    (other / "evil.txt").write_text("cross-owner bytes", encoding="utf-8")
    ref = _ref(vault_path=f"other/{AGENT}/attachments/fid1/evil.txt")
    out = _run(enrich_attachment_refs([ref], tenant_id=TENANT, agent_principal=AGENT))
    assert out[0]["extracted_text"] is None
    # no absolute path or raw bytes leak
    assert not str(out[0]["vault_path"]).startswith("/")


def test_cross_agent_ref_refused(vault_root):
    ref = _ref(vault_path=f"{TENANT}/agent:assistant:bob/attachments/fid1/evil.txt")
    out = _run(enrich_attachment_refs([ref], tenant_id=TENANT, agent_principal=AGENT))
    assert out[0]["extracted_text"] is None


def test_absolute_path_refused(vault_root):
    ref = _ref(vault_path="/etc/passwd")
    out = _run(enrich_attachment_refs([ref], tenant_id=TENANT, agent_principal=AGENT))
    assert out[0]["extracted_text"] is None
    assert not str(out[0]["vault_path"]).startswith("/")


def test_traversal_refused(vault_root):
    ref = _ref(vault_path=f"{TENANT}/{AGENT}/attachments/../../etc/passwd")
    out = _run(enrich_attachment_refs([ref], tenant_id=TENANT, agent_principal=AGENT))
    assert out[0]["extracted_text"] is None


def test_scheme_url_refused(vault_root):
    ref = _ref(vault_path="file:///etc/passwd")
    out = _run(enrich_attachment_refs([ref], tenant_id=TENANT, agent_principal=AGENT))
    assert out[0]["extracted_text"] is None


# ---------------------------------------------------------------------------
# extraction: bounded + masked
# ---------------------------------------------------------------------------

def test_extracted_text_bounded_and_masked(vault_root):
    secret_line = "api_key=TOPSECRET-12345\n"
    body = secret_line + ("lorem ipsum dolor sit amet " * 2000)  # > 20k chars
    (vault_root / TENANT / AGENT / "attachments" / "fid1" / "report.txt").write_text(body, encoding="utf-8")
    out = _run(enrich_attachment_refs([_ref()], tenant_id=TENANT, agent_principal=AGENT))
    ext = out[0]["extracted_text"]
    assert isinstance(ext, str) and ext.strip()
    assert len(ext) <= MAX_EXTRACTED_CHARS
    assert "TOPSECRET-12345" not in ext  # raw secret never forwarded
    assert "***" in ext  # masked instead


def test_json_secret_masked(vault_root):
    payload = '{"client_secret": "hunter2-hunter2", "note": "hello"}\n'
    (vault_root / TENANT / AGENT / "attachments" / "fid1" / "report.txt").write_text(payload, encoding="utf-8")
    out = _run(enrich_attachment_refs([_ref()], tenant_id=TENANT, agent_principal=AGENT))
    ext = out[0]["extracted_text"]
    assert ext is not None
    assert "hunter2" not in ext
    assert "***" in ext


def test_mask_secrets_unit():
    assert "s3cr3t" not in mask_secrets("password: s3cr3t")
    assert "***" in mask_secrets('{"access_token": "abc123"}')


def test_missing_file_is_metadata_only(vault_root):
    out = _run(enrich_attachment_refs([_ref()], tenant_id=TENANT, agent_principal=AGENT))
    assert out[0]["extracted_text"] is None
    assert out[0]["vault_path"] == f"{TENANT}/{AGENT}/attachments/fid1/report.txt"


def test_oversize_file_skipped(vault_root):
    (vault_root / TENANT / AGENT / "attachments" / "fid1" / "report.txt").write_bytes(b"x" * 64)
    out = _run(
        enrich_attachment_refs([_ref()], tenant_id=TENANT, agent_principal=AGENT, max_extract_bytes=16)
    )
    assert out[0]["extracted_text"] is None


# ---------------------------------------------------------------------------
# sensitive / non-extractable / images
# ---------------------------------------------------------------------------

def test_sensitive_ref_skipped_even_when_file_exists(vault_root):
    (vault_root / TENANT / AGENT / "attachments" / "fid1" / "report.txt").write_text(
        "perfectly innocent content", encoding="utf-8"
    )
    ref = _ref(reason="sensitive")
    out = _run(enrich_attachment_refs([ref], tenant_id=TENANT, agent_principal=AGENT))
    assert out[0]["extracted_text"] is None


def test_sensitive_filename_skipped(vault_root):
    (vault_root / TENANT / AGENT / "attachments" / "fid1" / "my_passwords.txt").write_text(
        "innocent", encoding="utf-8"
    )
    ref = _ref(filename="my_passwords.txt",
               vault_path=f"{TENANT}/{AGENT}/attachments/fid1/my_passwords.txt")
    out = _run(enrich_attachment_refs([ref], tenant_id=TENANT, agent_principal=AGENT))
    assert out[0]["extracted_text"] is None


def test_non_extractable_is_metadata_only(vault_root):
    blob = vault_root / TENANT / AGENT / "attachments" / "fid1" / "clip.zip"
    blob.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    ref = _ref(filename="clip.zip",
               vault_path=f"{TENANT}/{AGENT}/attachments/fid1/clip.zip",
               mime_type="application/zip", extractable=False, extract_hint="zip")
    out = _run(enrich_attachment_refs([ref], tenant_id=TENANT, agent_principal=AGENT))
    assert out[0]["extracted_text"] is None
    assert out[0]["filename"] == "clip.zip"
    assert out[0]["mime_type"] == "application/zip"


def test_image_ref_preserved_without_extraction(vault_root):
    ref = {
        "file_id": "img1", "attachment_id": "img1", "kind": "image",
        "filename": "a.png", "mime_type": "image/png", "size": 24,
        "source": "mattermost",
        "vault_path": f"{TENANT}/{AGENT}/attachments/img1/a.png",
        "url": "https://chat.example.com/api/v4/files/img1",
        "local_path": "/home/svc/.hermes/cache/img1_a.png",
        "data_url": "data:image/png;base64,iVBORw0KGgo=",
        "stored": True, "extractable": False, "extract_hint": "image",
        "extracted_text": None,
    }
    out = _run(enrich_attachment_refs([ref], tenant_id=TENANT, agent_principal=AGENT))
    assert out[0]["kind"] == "image"
    assert out[0]["extracted_text"] is None
    # image gate input preserved …
    assert out[0]["data_url"].startswith("data:image/png;base64,")
    # … while leak fields are stripped (no Mattermost URL, no absolute path)
    assert "url" not in out[0]
    assert "local_path" not in out[0]


def test_no_raw_bytes_or_token_leak(vault_root):
    (vault_root / TENANT / AGENT / "attachments" / "fid1" / "report.txt").write_text("hi", encoding="utf-8")
    ref = _ref(preview="should-be-stripped", base64="aGk=",
               data_url="data:text/plain;base64,aGk=",
               url="https://chat.example.com/api/v4/files/fid1",
               local_path="/tmp/report.txt")
    out = _run(enrich_attachment_refs([ref], tenant_id=TENANT, agent_principal=AGENT))
    for leak in ("preview", "base64", "data_url", "url", "local_path", "token", "bytes"):
        assert leak not in out[0], leak


# ---------------------------------------------------------------------------
# handler import / compile
# ---------------------------------------------------------------------------

def test_webhook_and_acp_handlers_import():
    import control_plane.mattermost_adapter.webhook as webhook  # noqa: F401
    import control_plane.mattermost_adapter.attachment_extract as extractor_mod  # noqa: F401
    import control_plane.acp_adapter as acp  # noqa: F401
    assert callable(extractor_mod.enrich_attachment_refs)
    assert hasattr(webhook, "_handle_core_logic_unserialized")
    assert hasattr(acp.ACPAdapter, "build_llm_messages")
