"""Vault streaming attachment store + bridge stored-ref regression.

- Vault: normal store/hash/readback, traversal rejection, cap/no-partial,
  atomic .part result + mode 0600. 500MB bound is the full upload/storage
  cap (ATTACHMENT_MAX_BYTES == 524288000), never LLM transfer.
- Bridge: STORED_ONLY refs (PDF/sensitive) carry owner-scoped relative
  vault_path + stored/sha256/size and NEVER preview/base64/data_url;
  images keep the bounded 20MB data_url path decoupled from durable store.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG_WIKI = REPO / "packages" / "personal-wiki"
SCRIPT = REPO / "scripts" / "oaos-mm-bridge.py"

if str(PKG_WIKI) not in sys.path:
    sys.path.insert(0, str(PKG_WIKI))

from personal_wiki.vault import (  # noqa: E402
    ATTACHMENT_MAX_BYTES,
    AttachmentTooLargeError,
    get_vault_root,
    sanitize_attachment_filename,
    store_attachment,
)


def _load_bridge():
    spec = importlib.util.spec_from_file_location("oaos_bridge_vault_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore
    return module


# ---------------------------------------------------------------------------
# Vault store_attachment
# ---------------------------------------------------------------------------

def test_attachment_max_is_500mb():
    assert ATTACHMENT_MAX_BYTES == 524288000


def test_store_normal_hash_readback(tmp_path):
    root = tmp_path / "vault"
    payload = b"hello-oaos-attachment" * 1000  # ~21KB
    meta = store_attachment(
        "default", "agent:assistant:alice", "report.pdf", payload,
        file_id="abc123", vault_root=root,
    )
    assert meta["stored"] is True
    assert meta["size"] == len(payload)
    assert meta["sha256"] == hashlib.sha256(payload).hexdigest()
    assert meta["filename"] == "report.pdf"
    vp = meta["vault_path"]
    assert not os.path.isabs(vp) and not vp.startswith("file://")
    assert vp.startswith("default/agent:assistant:alice/attachments/abc123/")
    dest = root / vp
    assert dest.is_file()
    assert dest.read_bytes() == payload
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    # no .part leftovers
    assert list(dest.parent.glob("*.part*")) == []


def test_store_streaming_chunks_and_file_like(tmp_path):
    root = tmp_path / "vault2"
    chunks = [b"a" * 70000, b"b" * 70000, b"c" * 1000]

    def gen():
        for c in chunks:
            yield c

    meta = store_attachment("t1", "a1", "big.bin", gen(), vault_root=root)
    raw = b"".join(chunks)
    assert meta["size"] == len(raw)
    assert meta["sha256"] == hashlib.sha256(raw).hexdigest()
    assert (root / meta["vault_path"]).read_bytes() == raw

    meta2 = store_attachment("t1", "a1", "via-fp.bin", io.BytesIO(raw), vault_root=root)
    assert meta2["sha256"] == hashlib.sha256(raw).hexdigest()
    assert (root / meta2["vault_path"]).read_bytes() == raw


def test_traversal_rejection(tmp_path):
    root = tmp_path / "vault3"
    with pytest.raises(Exception):
        store_attachment("../evil", "a1", "x.txt", b"hi", vault_root=root)
    with pytest.raises(Exception):
        store_attachment("t1", "../../etc", "x.txt", b"hi", vault_root=root)
    with pytest.raises(Exception):
        store_attachment("t1/a", "a1", "x.txt", b"hi", vault_root=root)
    # traversal filename is sanitized into the owner dir, never escapes
    meta = store_attachment("t1", "a1", "../../etc/passwd", b"secret", vault_root=root)
    dest = (root / meta["vault_path"]).resolve()
    assert str(dest).startswith(str(root.resolve()))
    assert ".." not in meta["vault_path"].split("/")
    assert sanitize_attachment_filename("../../etc/passwd") == meta["filename"]
    # nothing escaped the vault root
    assert not (tmp_path / "etc").exists()
    assert not (root.parent / "etc").exists()


def test_cap_exceeded_deletes_partial(tmp_path):
    root = tmp_path / "vault4"
    with pytest.raises((AttachmentTooLargeError, ValueError)):
        store_attachment("t1", "a1", "over.bin", b"x" * 20, vault_root=root, max_bytes=10)
    owner_dir = root / "t1" / "a1" / "attachments"
    leftovers = list(owner_dir.rglob("over.bin*")) if owner_dir.exists() else []
    # no final file and no .part partial
    assert leftovers == []
    assert list(root.rglob("*.part*")) == []


def test_atomic_replace_and_mode(tmp_path):
    root = tmp_path / "vault5"
    m1 = store_attachment("t1", "a1", "doc.txt", b"v1-content", vault_root=root)
    p = root / m1["vault_path"]
    assert p.read_bytes() == b"v1-content"
    m2 = store_attachment("t1", "a1", "doc.txt", b"v2-longer-content", vault_root=root)
    assert (root / m2["vault_path"]).read_bytes() == b"v2-longer-content"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert list(root.rglob("*.part*")) == []


def test_stream_read_error_propagates_and_deletes_partial(tmp_path):
    root = tmp_path / "vault6"

    class FlakyReader:
        def __init__(self):
            self.calls = 0

        def read(self, n=-1):
            self.calls += 1
            if self.calls == 1:
                return b"partial-data"
            raise OSError("simulated stream failure")

    with pytest.raises(OSError):
        store_attachment("t1", "a1", "flaky.bin", FlakyReader(), vault_root=root)
    owner_dir = root / "t1" / "a1" / "attachments"
    leftovers = list(owner_dir.rglob("flaky.bin*")) if owner_dir.exists() else []
    assert leftovers == []
    assert list(root.rglob("*.part*")) == []


def test_iterable_error_propagates_and_deletes_partial(tmp_path):
    root = tmp_path / "vault7"

    def bad_gen():
        yield b"good-chunk"
        raise OSError("simulated generator failure")

    with pytest.raises(OSError):
        store_attachment("t1", "a1", "badgen.bin", bad_gen(), vault_root=root)
    assert list(root.rglob("*.part*")) == []
    assert not (root / "t1" / "a1" / "attachments" / "badgen.bin").exists()


# ---------------------------------------------------------------------------
# Bridge regression: stored refs carry vault metadata, never LLM bytes
# ---------------------------------------------------------------------------

def _fake_vault_meta(fid="abc123", size=1234):
    return {
        "stored": True,
        "vault_path": f"default/agent:assistant:alice/attachments/{fid}/doc.pdf",
        "filename": "doc.pdf",
        "size": size,
        "sha256": hashlib.sha256(b"x" * size).hexdigest() if size < 100000 else "fakesha",
        "tenant_id": "default",
        "agent_id": "agent:assistant:alice",
    }


def test_bridge_pdf_stored_only_has_no_bytes(monkeypatch):
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_persist_attachment_to_vault",
                        lambda fid, fn, tenant="default", agent="": _fake_vault_meta(fid, 4321))
    ref = bridge._build_non_image_ref(
        "abc123", "doc.pdf", "application/pdf", 4321,
        "default", "agent:assistant:alice",
    )
    assert ref["kind"] == "stored_only"
    assert "preview" not in ref and "base64" not in ref and "data_url" not in ref
    assert ref["stored"] is True
    assert ref["sha256"]
    assert ref["size"] == 4321
    assert ref["vault_path"].startswith("default/agent:assistant:alice/attachments/")
    assert not ref["vault_path"].startswith("/") and "file://" not in ref["vault_path"]


def test_bridge_sensitive_still_stored_but_no_preview(monkeypatch):
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_persist_attachment_to_vault",
                        lambda fid, fn, tenant="default", agent="": _fake_vault_meta(fid, 100))
    ref = bridge._build_non_image_ref(
        "abc123", "client_secret_123.json", "application/json", 100,
        "default", "agent:assistant:alice",
    )
    assert ref["kind"] == "stored_only"
    assert ref["reason"] == "sensitive"
    assert "preview" not in ref and "base64" not in ref and "data_url" not in ref
    # policy: original preserved in owner vault, never exposed to preview/LLM
    assert ref["stored"] is True
    assert ref["vault_path"].startswith("default/agent:assistant:alice/")


def test_bridge_text_preview_masked_with_vault_meta(monkeypatch):
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_persist_attachment_to_vault",
                        lambda fid, fn, tenant="default", agent="": {
                            "stored": True,
                            "vault_path": f"default/agent:assistant:alice/attachments/{fid}/note.txt",
                            "size": 25, "sha256": "abc123sha",
                            "tenant_id": "default", "agent_id": "agent:assistant:alice",
                        })
    monkeypatch.setattr(bridge, "_download_capped_bytes",
                        lambda fid, cap: (b'hello world build log line 1\nline2', False))
    ref = bridge._build_non_image_ref(
        "abc123", "note.txt", "text/plain", 25,
        "default", "agent:assistant:alice",
    )
    assert ref["kind"] == "text_preview"
    assert "line 1" in ref["preview"]
    assert ref["stored"] is True and ref["sha256"] == "abc123sha"
    assert "base64" not in ref and "data_url" not in ref
    # masking defense-in-depth never leaks raw secret values to the LLM
    assert "abc123SECRET" not in bridge._mask_secrets('{"api_key": "abc123SECRET"}')


def test_bridge_image_retains_data_url_decoupled_from_store(monkeypatch):
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "get_mattermost_file_info",
                        lambda fid: {"name": "a.png", "mime_type": "image/png", "size": 8})
    monkeypatch.setattr(bridge, "_download_mattermost_file_bytes",
                        lambda fid: (b"\x89PNG1234", "image/png"))
    monkeypatch.setattr(bridge, "_persist_attachment_to_vault",
                        lambda fid, fn, tenant="default", agent="": {
                            "stored": True,
                            "vault_path": f"{tenant}/{agent}/attachments/{fid}/a.png",
                            "size": 8, "sha256": "imgsha",
                            "tenant_id": tenant, "agent_id": agent,
                        })
    post = {"file_ids": ["abc123"], "metadata": {}}
    file_ids, refs = bridge.build_attachment_refs_for_post(
        post, tenant_id="default",
        employee="employee:alice", agent="agent:assistant:alice",
    )
    assert file_ids == ["abc123"]
    assert len(refs) == 1
    ref = refs[0]
    assert ref["kind"] == "image"
    assert ref.get("data_url", "").startswith("data:image/png;base64,")
    assert ref["stored"] is True and ref["sha256"] == "imgsha"
    assert ref["vault_path"].startswith("default/agent:assistant:alice/attachments/")
    assert not ref["vault_path"].startswith("/") and "file://" not in ref["vault_path"]


def test_bridge_over_limit_owner_path_and_no_store(monkeypatch):
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "get_mattermost_file_info",
                        lambda fid: {"name": "huge.pdf", "mime_type": "application/pdf",
                                     "size": bridge.MAX_TOTAL_ATTACHMENT_BYTES + 1})
    file_ids, refs = bridge.build_attachment_refs_for_post(
        {"file_ids": ["abc123"], "metadata": {}},
        tenant_id="default", employee="employee:alice", agent="agent:assistant:alice",
    )
    assert refs[0]["reason"] == "over_limit"
    assert refs[0]["stored"] is False
    assert refs[0]["vault_path"].startswith("default/agent:assistant:alice/")
    assert "preview" not in refs[0] and "data_url" not in refs[0]
