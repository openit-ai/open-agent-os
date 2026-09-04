#!/usr/bin/env python3
"""
OAOS Mattermost Standard Bridge — polls DM channels as @agent bot and forwards to Control Plane
POST /v1/mattermost/events (standard adapter flow). Fallback to Ollama if Control Plane stream empty.

Replaces oaos-agent-poller.py. Uses standard Control Plane + MattermostAdapter for replies.
Keeps hermes @openit untouched (hermes no longer on Mattermost).

Attachment slice (2026-08-30): images sent from Mattermost are forwarded through the current
conversation's Agent Runtime (ACP/Hermes) — no separate LLM/model/provider and no OCR.
Builds attachment_refs with file_id/filename/mime_type/size and a safe vault/local reference,
and passes file_ids/attachment_refs/runtime_context in the signed CP payload.
"""
import os, json, time, pathlib, re, hmac, hashlib, threading, base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request, urllib.error

# Bound per-poll I/O concurrency so one slow user/channel cannot stall others.
POLL_CHANNEL_WORKERS = 4


def fetch_channel_posts_parallel(channels, max_workers=POLL_CHANNEL_WORKERS):
    """Fetch DM channel posts concurrently with a bounded worker pool."""
    def _fetch(channel):
        cid = channel["id"]
        try:
            return cid, api_get(f"/api/v4/channels/{cid}/posts?page=0&per_page=20")
        except Exception as exc:
            print(f"[poll] posts failed channel={cid[:6]} err={str(exc)[:120]}", flush=True)
            return cid, None

    results = {}
    worker_count = max(1, min(int(max_workers), POLL_CHANNEL_WORKERS))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="mm-poll") as pool:
        futures = [pool.submit(_fetch, channel) for channel in channels]
        for future in as_completed(futures):
            cid, data = future.result()
            if data is not None:
                results[cid] = data
    return results

# Vision inference can exceed the text path's latency; allow bounded read-back.
POST_CONFIRM_TIMEOUT_S = 240
POST_CONFIRM_INTERVAL_S = 3


def _is_bot_reply_for_root(
    post: dict,
    bot_id: str,
    root_id: str,
    source_post_id: str,
    source_create_at: int = 0,
) -> bool:
    """Return True only for a bot reply in the same thread, not the source, and created after source.

    - Keeps root post and child post semantics: (post.root_id or post.id) must equal root_id.
    - Accepts source_create_at as int epoch ms or as a source post dict containing create_at/id.
    - If source_create_at is truthy, candidate must have create_at > source_create_at.
    """
    # Back-compat: allow source post dict passed as 5th arg or as 4th arg overloaded
    if isinstance(source_create_at, dict):
        try:
            source_create_at = int(source_create_at.get("create_at") or 0)
        except Exception:
            source_create_at = 0
    if isinstance(source_post_id, dict):
        # overloaded call wait_for_bot_reply(..., source_post_dict)
        src = source_post_id
        try:
            source_create_at = int(src.get("create_at") or source_create_at or 0)
        except Exception:
            pass
        source_post_id = str(src.get("id") or "")

    if post.get("user_id") != bot_id:
        return False
    if post.get("id") == source_post_id:
        return False
    # Root vs child semantics: thread root must match
    if (post.get("root_id") or post.get("id")) != root_id:
        return False
    # Timestamp guard: only accept replies created strictly after source
    if source_create_at:
        try:
            cand_at = int(post.get("create_at") or 0)
        except Exception:
            cand_at = 0
        if not cand_at or cand_at <= int(source_create_at):
            return False
    return True


def wait_for_bot_reply(
    channel_id: str,
    bot_id: str,
    root_id: str,
    source_post_id: str,
    source_create_at: int = 0,
) -> str:
    """Poll long enough for the Gateway LLM, confirming the exact thread.

    New contract: caller passes source post creation timestamp (or source post object)
    so only a bot reply created strictly after the source is accepted. Keeps root/child
    thread semantics via _is_bot_reply_for_root.
    """
    # Normalize overloaded forms: source_post dict as 4th or 5th arg
    if isinstance(source_post_id, dict):
        src = source_post_id
        try:
            source_create_at = int(src.get("create_at") or source_create_at or 0)
        except Exception:
            pass
        source_post_id = str(src.get("id") or "")
    if isinstance(source_create_at, dict):
        try:
            source_create_at = int(source_create_at.get("create_at") or 0)
        except Exception:
            source_create_at = 0

    deadline = time.monotonic() + POST_CONFIRM_TIMEOUT_S
    while time.monotonic() < deadline:
        chk = api_get(f"/api/v4/channels/{channel_id}/posts?page=0&per_page=20")
        for candidate_id in chk.get("order", []):
            candidate = chk.get("posts", {}).get(candidate_id, {})
            if _is_bot_reply_for_root(candidate, bot_id, root_id, source_post_id, source_create_at):
                return candidate_id
        time.sleep(POST_CONFIRM_INTERVAL_S)
    return ""

CP_WEBHOOK_SECRET = os.getenv("OAOS_CP_MATTERMOST_WEBHOOK_SECRET", "")


def _load_cp_secret():
    global CP_WEBHOOK_SECRET
    if CP_WEBHOOK_SECRET:
        return CP_WEBHOOK_SECRET
    try:
        for line in pathlib.Path.home().joinpath(".config/oaos.env").read_text().splitlines():
            if line.startswith("OAOS_CP_MATTERMOST_WEBHOOK_SECRET="):
                CP_WEBHOOK_SECRET = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    except OSError:
        pass
    return CP_WEBHOOK_SECRET

def _signature(body):
    secret = _load_cp_secret()
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest() if secret else ""

MATTERMOST_URL = os.getenv("MATTERMOST_URL", "https://chat.openit.co.kr")
BOT_TOKEN = os.getenv("MATTERMOST_BOT_TOKEN", "")
if not BOT_TOKEN or len(BOT_TOKEN) < 20:
    try:
        for line in pathlib.Path.home().joinpath(".hermes/.env").read_text().splitlines():
            if line.startswith("MATTERMOST_BOT_TOKEN="):
                BOT_TOKEN = line.split("=",1)[1].strip().strip('"').strip("'")
    except: pass

CONTROL_PLANE = os.getenv("OAOS_CONTROL_PLANE_URL", "http://127.0.0.1:8100")
SEEN_FILE = pathlib.Path.home() / ".hermes/cache/oaos-mm-bridge-seen.json"
BOT_ID = os.getenv("MATTERMOST_BOT_ID", "")
# Never fall back to another server's bot id: resolve from /users/me at startup (fail-closed).
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")

# ── Attachment forwarding (image via Agent Runtime, no OCR/model/provider) ──
IMAGE_MIME_PREFIX = "image/"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".svg", ".heic", ".heif"}
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
# bounded data URL: same limit for decoded bytes; encoded ~33% larger but still enforced via decoded size
MAX_IMAGE_DATA_URL_BYTES = MAX_ATTACHMENT_BYTES
ALLOWED_IMAGE_MIMES = {
    "image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp",
    "image/bmp", "image/tiff", "image/svg+xml", "image/heic", "image/heif",
}
ATTACH_CACHE_DIR = pathlib.Path.home() / ".hermes/cache/oaos-mm-attachments"

# ── Owner-scoped Vault durable store (streaming, never full 500MB in memory) ──
# Live vault root is OAOS_WIKI_VAULT (vault.get_vault_root() honors it + fallbacks).
# Bridge streams each attachment in 64KB units directly into
# personal_wiki.vault.store_attachment (tenant/agent owner path, sha256, 0600,
# atomic .part->os.replace, 500MB cap). Import is lazy/file-located so unit
# tests and vault-less environments keep the old logical-path fallback.
VAULT_STREAM_CHUNK = 65536  # 64KB — streaming unit for durable store
VAULT_DOWNLOAD_TIMEOUT_S = 60

# Formats the CP may later extract with a bounded extractor (pdf/docx/xlsx/pptx…).
# Bridge only marks extractability; it never runs large/async extraction itself.
# CP fills optional ref["extracted_text"] (bounded, masked) for prompt use.
EXTRACTABLE_EXTENSIONS = frozenset({
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".hwp", ".hwpx", ".txt", ".md", ".markdown", ".csv", ".tsv",
    ".json", ".jsonl", ".xml", ".yaml", ".yml", ".log",
})

_VAULT_MOD_CACHE: dict = {}

# Mattermost file ids are lowercase alnum, but accept the wider safe segment
# shape everywhere (vault allowlist) so persist/preview/image paths agree.
_FID_RE = r"^[A-Za-z0-9_-]{1,128}$"
_FID_MM_RE = r"^[a-z0-9]+$"

# ── Attachment router: IMAGE / TEXT_PREVIEW / STORED_ONLY ──
# 500MB 한도까지 저장·활용하되 500MB 원본을 LLM에 직전송하지 않는다.
# 바이트·base64·data_url은 IMAGE만, 미리보기는 마스킹된 텍스트만,
# 원문 비밀은 절대 로그·전송하지 않는다.
MAX_TOTAL_ATTACHMENT_BYTES = 500 * 1024 * 1024  # 524288000
MAX_TEXT_PREVIEW_BYTES = 200 * 1024  # 204800

TEXT_PREVIEW_MIMES = {
    "text/plain", "text/markdown", "text/csv", "text/html", "text/xml",
    "text/yaml", "text/x-log", "application/json", "application/xml",
    "application/javascript", "application/x-javascript", "application/x-yaml",
    "application/x-sh", "application/x-python", "application/sql",
}
TEXT_PREVIEW_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".jsonl",
    ".xml", ".yaml", ".yml", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".java", ".c", ".h", ".cpp", ".hpp", ".go", ".rs", ".sh", ".bash",
    ".sql", ".css", ".html", ".htm", ".ini", ".cfg", ".toml",
}

# 민감 판정: 파일명·미리보기에서 이 패턴이 보이면 미리보기 금지 → STORED_ONLY + 안전 안내
_SENSITIVE_KEY_RE = re.compile(
    r"client_secret|private[\s_\-]*key|refresh[\s_\-]*token|access[\s_\-]*token|api[\s_\-]*key|passw(?:or)?d",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(client_secret|private[\s_\-]*key|refresh[\s_\-]*token|access[\s_\-]*token|api[\s_\-]*key|passw(?:or)?d)\s*([:=]|=>)\s*\S+",
    re.IGNORECASE,
)
_SECRET_JSON_VALUE_RE = re.compile(
    r'("(?:client_secret|private[\s_\-]*key|refresh[\s_\-]*token|access[\s_\-]*token|api[\s_\-]*key|passw(?:or)?d)"\s*:\s*")[^"]*(")',
    re.IGNORECASE,
)

ATTACHMENT_FORMAT_GUIDANCE = (
    "첨부 파일 형식 안내: 이미지는 바로 확인할 수 있고, 텍스트·코드·CSV 등은 "
    "미리보기(최대 200KB, 민감 정보 제외)로 확인합니다. 그 외 형식(PDF·Office·"
    "음성·영상·압축 등)은 내용 확인이 어려우니 이미지 또는 텍스트로 다시 보내 주세요."
)


def _mask_secrets(text: str) -> str:
    """Mask secret values (key=value and JSON quoted shapes) so raw secrets never reach logs or the LLM."""
    try:
        masked = _SECRET_JSON_VALUE_RE.sub(lambda m: f"{m.group(1)}***{m.group(2)}", text or "")
        return _SECRET_VALUE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", masked)
    except Exception:
        return text or ""


def _format_bytes(n) -> str:
    try:
        n = int(n or 0)
    except Exception:
        return "size unknown"
    if n <= 0:
        return "0B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}GB"


def _is_text_previewable(mime: str, filename: str) -> bool:
    """Text-decodable formats only (text/code/csv/md/log/small json etc.)."""
    m = (mime or "").strip().lower().split(";")[0].strip()
    if m in TEXT_PREVIEW_MIMES or m.startswith("text/"):
        return True
    ext = pathlib.Path(filename or "").suffix.lower()
    return ext in TEXT_PREVIEW_EXTENSIONS


def _resolve_bridge_tenant(explicit: str | None = None) -> str:
    """Bridge-side tenant matching the CP server authority (settings.tenant_id).

    The CP webhook ignores payload tenant_id and uses its server-configured
    tenant for sessions AND for owner validation of vault_path. The bridge
    embeds tenant in every vault_path it emits, so it must use the same
    value or every ref fails CP cross-owner validation (extraction unusable).
    Precedence: explicit arg > OAOS_CP_TENANT_ID > OAOS_TENANT_ID > TENANT_ID.
    """
    for cand in (explicit, os.getenv("OAOS_CP_TENANT_ID"), os.getenv("OAOS_TENANT_ID"), os.getenv("TENANT_ID")):
        if cand and str(cand).strip():
            return str(cand).strip()
    return "default"


def _download_capped_bytes(file_id: str, cap: int) -> tuple[bytes | None, bool]:
    """Bounded download without MIME enforcement. Returns (bytes, truncated)."""
    fid = (file_id or "").strip()
    if not fid or not re.match(_FID_RE, fid):
        return None, False
    url = f"{MATTERMOST_URL}/api/v4/files/{fid}"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {BOT_TOKEN}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read(cap + 1)
            if len(data) > cap:
                return data[:cap], True
            if not data:
                return None, False
            return data, False
    except Exception as e:
        print(f"[attach] preview download failed fid={fid[:6]} err={str(e)[:120]}", flush=True)
        return None, False


def _build_non_image_ref(
    fid: str, filename: str, mime: str, size: int,
    tenant_id: str = "default", agent_id: str = "",
) -> dict:
    """Route a non-image file to TEXT_PREVIEW or STORED_ONLY with durable Vault bytes.

    - Original bytes are streamed (Bearer, 64KB) into the owner-scoped Vault
      (tenant/agent) even for sensitive/unsupported formats — policy allows
      preservation in the owner vault while preview/LLM exposure is forbidden.
    - TEXT_PREVIEW keeps the bounded 200KB masked preview; STORED_ONLY refs
      carry metadata only (no preview/base64/data_url) plus owner-scoped
      relative vault_path, stored, sha256, actual size.
    - PDF/Office/HWP etc. get extractable/extract_hint markers so the CP may
      run a bounded extractor later; the bridge never extracts here.
    """
    mime_disp = (mime or "").strip().lower().split(";")[0].strip() or "unknown"
    safe_name = _sanitize_filename(filename or fid)
    # Durable owner-vault store first (streaming; never full file in memory).
    vault_meta = None
    if agent_id:
        try:
            vault_meta = _persist_attachment_to_vault(fid, filename or fid, tenant_id, agent_id)
        except Exception:
            vault_meta = None
    if vault_meta and isinstance(vault_meta, dict) and vault_meta.get("stored"):
        vault_path = str(vault_meta.get("vault_path") or "")
        actual_size = int(vault_meta.get("size") or size or 0)
        sha256 = str(vault_meta.get("sha256") or "")
        stored = True
    elif isinstance(vault_meta, dict) and vault_meta.get("over_limit"):
        # Durable stream proved the bytes exceed the 500MB cap: metadata-only,
        # never download a preview bypass.
        return {
            "file_id": fid,
            "attachment_id": fid,
            "kind": "stored_only",
            "filename": safe_name,
            "mime_type": mime_disp,
            "size": int(vault_meta.get("size") or size or 0),
            "source": "mattermost",
            "vault_path": str(vault_meta.get("vault_path") or _owner_vault_fallback(tenant_id, agent_id, fid, filename or fid)),
            "stored": False,
            "reason": "over_limit",
            "extractable": _is_extractable_hint(mime, filename or ""),
            "extract_hint": (pathlib.Path(filename or "").suffix.lower().lstrip(".") or mime_disp),
            "extracted_text": None,
        }
    else:
        vault_path = _owner_vault_fallback(tenant_id, agent_id, fid, filename or fid)
        actual_size = int(size or 0)
        sha256 = ""
        stored = bool(vault_meta and vault_meta.get("stored"))
    base = {
        "file_id": fid,
        "attachment_id": fid,
        "kind": "stored_only",
        "filename": safe_name,
        "mime_type": mime_disp,
        "size": actual_size,
        "source": "mattermost",
        "vault_path": vault_path,
        "stored": stored,
        "extractable": _is_extractable_hint(mime, filename or ""),
        "extract_hint": (pathlib.Path(filename or "").suffix.lower().lstrip(".") or mime_disp),
        "extracted_text": None,
    }
    if sha256:
        base["sha256"] = sha256

    def _stored(reason: str) -> dict:
        ref = dict(base)
        ref["reason"] = reason
        # stored_only metadata only: never preview/base64/data_url bytes
        ref.pop("preview", None)
        ref.pop("base64", None)
        ref.pop("data_url", None)
        return ref

    # sensitive filename → preview forbidden, no preview download (vault bytes already preserved)
    if _SENSITIVE_KEY_RE.search(filename or ""):
        print(f"[attach] preview blocked sensitive name fid={fid[:6]}", flush=True)
        return _stored("sensitive")
    if not _is_text_previewable(mime, filename):
        return _stored("unsupported")
    # text preview path: serve from the just-stored vault copy when available
    # (single-download: durable stream already fetched the bytes), else one
    # bounded 200KB HTTP fetch. Decode + sensitive-content checks stay identical.
    data: bytes | None = None
    truncated = False
    if stored:
        try:
            _vb = _read_vault_prefix_bytes(vault_path, MAX_TEXT_PREVIEW_BYTES)
        except Exception:
            _vb = None
        if isinstance(_vb, bytes) and _vb:
            if len(_vb) > MAX_TEXT_PREVIEW_BYTES:
                data, truncated = _vb[:MAX_TEXT_PREVIEW_BYTES], True
            else:
                data, truncated = _vb, False
    if data is None:
        data, truncated = _download_capped_bytes(fid, MAX_TEXT_PREVIEW_BYTES)
    if data is None:
        return _stored("preview_unavailable")
    if b"\x00" in data:
        return _stored("unsupported")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _stored("unsupported")
    if _SENSITIVE_KEY_RE.search(text):
        # never log or transmit the raw preview
        print(f"[attach] preview blocked sensitive content fid={fid[:6]}", flush=True)
        return _stored("sensitive")
    ref = dict(base)
    ref["kind"] = "text_preview"
    # No MM API url: CP resolves bytes via owner vault_path; the Bearer URL
    # (host + fid) must not travel to the LLM path.
    ref["preview"] = _mask_secrets(text)
    ref["truncated"] = bool(truncated)
    return ref


def _preview_block(ref: dict) -> str:
    fname = ref.get("filename") or "attachment"
    mime = ref.get("mime_type") or "unknown"
    trunc = " (앞부분 200KB만 표시)" if ref.get("truncated") else ""
    return f"[첨부 미리보기: {fname} ({mime}, {_format_bytes(ref.get('size'))}){trunc}]\n{ref.get('preview') or ''}"


def _stored_note(ref: dict) -> str:
    fname = _mask_secrets(str(ref.get("filename") or "첨부 파일"))
    mime = ref.get("mime_type") or "unknown"
    reason = ref.get("reason") or "unsupported"
    if reason == "sensitive":
        return (
            f"[참고: 첨부 {fname}은 민감 정보가 포함될 수 있어 미리보기를 생략합니다. "
            f"필요한 부분만 텍스트로 보내 주세요.]"
        )
    if reason == "over_limit":
        return (
            f"[참고: 첨부 {fname} ({_format_bytes(ref.get('size'))})은 500MB 한도를 초과하여 "
            f"내용 없이 메타데이터만 전달됩니다.]"
        )
    return (
        f"[참고: 첨부 {fname} ({mime})은 현재 내용 확인이 어려워 메타데이터만 전달됩니다. "
        f"이미지 또는 텍스트로 보내 주시면 확인해 드리겠습니다.]"
    )


def _load_vault_module():
    """Lazy-load personal_wiki.vault.store_attachment via file location (cached).

    Returns module or None when unavailable — callers fall back to a logical
    owner-scoped vault_path without bytes. Never raises.
    """
    try:
        if _VAULT_MOD_CACHE.get("mod") is not None:
            return _VAULT_MOD_CACHE["mod"]
    except Exception:
        pass
    try:
        import importlib.util as _ilu
        here = pathlib.Path(__file__).resolve()
        cands = [
            here.parents[1] / "packages" / "personal-wiki" / "personal_wiki" / "vault.py",
            here.parent / "packages" / "personal-wiki" / "personal_wiki" / "vault.py",
            pathlib.Path.cwd() / "packages" / "personal-wiki" / "personal_wiki" / "vault.py",
        ]
        target = next((c for c in cands if c.exists()), None)
        if target is None:
            # also try importable package path
            try:
                import personal_wiki.vault as _pv  # type: ignore
                _VAULT_MOD_CACHE["mod"] = _pv
                return _pv
            except Exception:
                _VAULT_MOD_CACHE["mod"] = None
                return None
        spec = _ilu.spec_from_file_location("oaos_bridge_vault", str(target))
        if not spec or not spec.loader:
            _VAULT_MOD_CACHE["mod"] = None
            return None
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        _VAULT_MOD_CACHE["mod"] = mod
        return mod
    except Exception:
        try:
            _VAULT_MOD_CACHE["mod"] = None
        except Exception:
            pass
        return None


def _owner_vault_fallback(tenant_id: str, agent_id: str, fid: str, filename: str) -> str:
    """Owner-scoped relative vault_path used when durable store is unavailable."""
    t = (tenant_id or "default").strip() or "default"
    a = (agent_id or "").strip() or "agent"
    # keep single-segment safety without importing vault (colons preserved)
    t = re.sub(r"[^A-Za-z0-9:._-]", "_", t)[:64] or "default"
    a = re.sub(r"[^A-Za-z0-9:._-]", "_", a)[:128] or "agent"
    # fid is a logical path segment too: hash odd ids to an isolated segment
    # instead of colliding every odd id onto "file/".
    fid_s = (fid or "").strip()
    if not re.match(r"^[A-Za-z0-9_-]{1,128}$", fid_s):
        import hashlib as _hl
        _dig = _hl.sha256(fid_s.encode("utf-8", errors="replace")).hexdigest()[:12] if fid_s else "empty"
        fid_s = f"file-{_dig}"
    return f"{t}/{a}/attachments/{fid_s}/{_sanitize_filename(filename or fid)}"


def _is_extractable_hint(mime: str, filename: str) -> bool:
    """True when CP-side bounded extractor may handle the format (bridge never extracts)."""
    ext = pathlib.Path(filename or "").suffix.lower()
    return ext in EXTRACTABLE_EXTENSIONS


def _persist_attachment_to_vault(
    file_id: str, filename: str, tenant_id: str = "default", agent_id: str = "",
) -> dict | None:
    """Stream one Mattermost file (Bearer, 64KB units) into the owner vault.

    Never holds the full file in memory: the HTTP response object is passed as
    the stream and vault.store_attachment reads it in 64KB chunks with a 500MB
    cap, incremental sha256, atomic .part->os.replace, mode 0600.
    Returns vault metadata (stored/vault_path/sha256/size) or None on any
    failure (fallback path is used; raw errors are truncated, no secrets).
    """
    fid = (file_id or "").strip()
    if not fid or not re.match(_FID_RE, fid):
        return None
    tenant = (tenant_id or "default").strip() or "default"
    agent = (agent_id or "").strip()
    if not agent:
        return None
    mod = _load_vault_module()
    if mod is None or not hasattr(mod, "store_attachment"):
        return None
    url = f"{MATTERMOST_URL}/api/v4/files/{fid}"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {BOT_TOKEN}"})
        # NOTE: response is streamed — vault reads .read(64KB); never resp.read() fully here.
        with urllib.request.urlopen(req, timeout=VAULT_DOWNLOAD_TIMEOUT_S) as resp:
            meta = mod.store_attachment(
                tenant, agent, filename or fid, resp,
                file_id=fid, max_bytes=MAX_TOTAL_ATTACHMENT_BYTES,
            )
            if isinstance(meta, dict) and meta.get("stored"):
                return meta
            return None
    except Exception as e:
        # Over-cap streams stay metadata-only: signal over_limit so callers skip
        # preview/data_url downloads instead of bypassing the cap via a 2nd fetch.
        # (class identity is by name: vault is loaded lazily via file location.)
        if type(e).__name__ == "AttachmentTooLargeError":
            try:
                _fb = _owner_vault_fallback(tenant, agent, fid, filename or fid)
            except Exception:
                _fb = f"{tenant}/{agent}/attachments/file/{_sanitize_filename(filename or fid)}"
            print(f"[attach] vault store over 500m cap fid={fid[:6]} — metadata only", flush=True)
            return {"stored": False, "over_limit": True, "vault_path": _fb,
                    "size": MAX_TOTAL_ATTACHMENT_BYTES + 1, "sha256": "",
                    "tenant_id": tenant, "agent_id": agent}
        # Other network/vault errors: metadata-only fallback (no secrets in log).
        print(f"[attach] vault store failed fid={fid[:6]} err={str(e)[:120]}", flush=True)
        return None


def _read_vault_prefix_bytes(vault_path: str, cap: int) -> bytes | None:
    """Read first cap+1 bytes from a stored owner-vault file (no network).

    Single-download optimization: after the streaming durable store succeeds,
    preview / image bytes are served from the local vault copy instead of a
    second Mattermost fetch. Fail-closed: any validation/IO error returns None
    so callers fall back to the bounded HTTP path or metadata-only.
    """
    try:
        vp = (vault_path or "").strip()
        if not vp:
            return None
        if vp.startswith("/") or vp.startswith("\\") or vp.startswith("file://") or "://" in vp:
            return None
        if pathlib.Path(vp).is_absolute():
            return None
        parts = vp.replace("\\", "/").split("/")
        if ".." in parts:
            return None
        if len(parts) < 4:
            return None
        try:
            cap_i = int(cap)
        except Exception:
            return None
        if cap_i <= 0 or cap_i > MAX_TOTAL_ATTACHMENT_BYTES:
            return None
        mod = _load_vault_module()
        root = None
        try:
            if mod is not None and hasattr(mod, "get_vault_root"):
                root = pathlib.Path(str(mod.get_vault_root()))
        except Exception:
            root = None
        if root is None:
            for _k in ("OAOS_WIKI_VAULT", "PERSONAL_WIKI_VAULT", "VAULT_ROOT"):
                try:
                    _v = os.getenv(_k, "").strip()
                except Exception:
                    _v = ""
                if _v:
                    root = pathlib.Path(_v).expanduser()
                    break
        if root is None:
            return None
        joined = root.joinpath(*parts)
        try:
            _rr = root.resolve()
            _jr = joined.resolve()
            if _jr != _rr and _rr not in _jr.parents:
                return None
        except Exception:
            return None
        try:
            if not _jr.is_file():
                return None
        except Exception:
            return None
        try:
            with open(_jr, "rb") as _f:
                return _f.read(cap_i + 1)
        except Exception:
            return None
    except Exception:
        return None

def _normalize_mime(mime: str) -> str:
    m = (mime or "").strip().lower().split(";")[0].strip()
    # alias
    if m == "image/jpg":
        m = "image/jpeg"
    return m or "image/png"

def _is_allowed_image_mime(mime: str) -> bool:
    m = _normalize_mime(mime)
    if m in ALLOWED_IMAGE_MIMES:
        return True
    # allow any image/* as fallback (future types), but block non-image
    return m.startswith(IMAGE_MIME_PREFIX)

def _is_image_mime(mime: str, filename: str) -> bool:
    m = (mime or "").lower()
    if m.startswith(IMAGE_MIME_PREFIX):
        return True
    ext = pathlib.Path(filename or "").suffix.lower()
    return ext in IMAGE_EXTENSIONS

def _sanitize_filename(name: str) -> str:
    # safe single-segment filename: keep alnum, dot, dash, underscore
    base = pathlib.Path(name or "attachment").name
    # strip NUL/controls first so they never reach logs, paths, or the LLM
    base = re.sub(r"[\x00-\x1f\x7f]", "_", base)
    base = base.strip().strip(".") or "attachment"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "attachment"
    # avoid path traversal
    safe = safe.replace("..", "_")
    safe = re.sub(r"_+", "_", safe)
    return safe[:180] or "attachment"

def get_mattermost_file_info(file_id: str) -> dict:
    """Fetch Mattermost file metadata safely (no secret logging). Returns normalized dict or {} on failure."""
    fid = (file_id or "").strip()
    if not fid or not re.match(_FID_RE, fid):
        return {}
    try:
        info = api_get(f"/api/v4/files/{fid}/info")
        # MM returns {id, user_id, post_id, create_at, update_at, delete_at, name, extension, size, mime_type, ...}
        if not isinstance(info, dict):
            return {}
        # some deployments return {"file_infos": [...]} or single; handle both
        if "file_infos" in info and isinstance(info["file_infos"], list) and info["file_infos"]:
            info = info["file_infos"][0]
        return info
    except Exception as e:
        print(f"[attach] file info failed fid={fid[:6]} err={str(e)[:120]}", flush=True)
        return {}

def _download_mattermost_file_bytes(file_id: str) -> tuple[bytes | None, str | None]:
    """Download raw file bytes via authenticated Mattermost API. Returns (bytes, mime) or (None, None) on failure/skip.

    Enforces MAX_ATTACHMENT_BYTES and image MIME validation. Uses Bearer BOT_TOKEN.
    """
    fid = (file_id or "").strip()
    if not fid or not re.match(_FID_RE, fid):
        return None, None
    url = f"{MATTERMOST_URL}/api/v4/files/{fid}"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {BOT_TOKEN}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            # MIME from header (authoritative) fallback to expected
            header_mime = ""
            try:
                header_mime = resp.getheader("Content-Type") or resp.headers.get("Content-Type", "") if hasattr(resp, "headers") else ""
            except Exception:
                header_mime = ""
            header_mime = _normalize_mime(header_mime) if header_mime else ""
            # Read with bound
            data = resp.read(MAX_ATTACHMENT_BYTES + 1)
            if len(data) > MAX_ATTACHMENT_BYTES:
                print(f"[attach] skip oversized download fid={fid[:6]} bytes={len(data)}", flush=True)
                return None, None
            if not data:
                print(f"[attach] empty bytes fid={fid[:6]}", flush=True)
                return None, None
            # MIME validation: require image/*
            effective_mime = header_mime or ""
            if effective_mime and not effective_mime.startswith(IMAGE_MIME_PREFIX):
                # header says non-image; double-check against allowlist (defense)
                print(f"[attach] skip non-image content-type fid={fid[:6]} mime={effective_mime}", flush=True)
                return None, None
            return data, effective_mime or ""
    except Exception as e:
        print(f"[attach] download failed fid={fid[:6]} err={str(e)[:160]}", flush=True)
        return None, None

def build_attachment_refs_for_post(
    post: dict, tenant_id: str = "default", employee: str = "", agent: str = "",
) -> tuple[list[str], list[dict]]:
    """Route post attachments: IMAGE / TEXT_PREVIEW / STORED_ONLY.

    - Uses post file_ids and optionally metadata.files
    - Fetches file info via api_get for mime/size validation
    - IMAGE: existing behavior fully kept (20MB cap, base64/data_url via Agent Runtime),
      PLUS a separate streaming durable store (Bearer, 64KB, 500MB cap) into the
      owner-scoped Vault — storage and LLM transfer are decoupled.
    - TEXT_PREVIEW: text-decodable formats only, max 200KB masked preview in CP text
    - STORED_ONLY: PDF/Office/HWP/audio/video/archive/large/sensitive — metadata only
      for the LLM, but original bytes are still streamed to the owner vault.
    - Every ref records owner-scoped relative vault_path, stored, sha256 (when
      stored), and actual size. vault_path is always relative — never absolute.
    - file_ids includes every routed file so file-only posts carry refs (no CP 400)
    - Raw secret values are never logged or transmitted (filenames masked in logs)
    - No OCR, no model/provider selection for images — only a safe vault/local reference
    - PDF/Office/HWP carry extractable/extract_hint for a future CP-side bounded
      extractor (ref["extracted_text"], None by default); the bridge never extracts.
    """
    raw_ids: list[str] = []
    # primary: post.file_ids
    if isinstance(post.get("file_ids"), list):
        raw_ids.extend([str(x) for x in post.get("file_ids") if x])
    # fallback: metadata.files[].id
    try:
        meta = post.get("metadata") or {}
        files = meta.get("files") if isinstance(meta, dict) else None
        if isinstance(files, list):
            for f in files:
                if isinstance(f, dict) and f.get("id"):
                    fid = str(f["id"])
                    if fid not in raw_ids:
                        raw_ids.append(fid)
    except Exception:
        pass
    if not raw_ids:
        return [], []
    file_ids: list[str] = []
    refs: list[dict] = []
    for fid in raw_ids:
        info = get_mattermost_file_info(fid)
        # if info fetch failed, try metadata fallback for filename/mime
        filename = ""
        mime = ""
        size = 0
        if info:
            filename = info.get("name") or info.get("filename") or ""
            mime = info.get("mime_type") or info.get("mimeType") or ""
            try:
                size = int(info.get("size") or 0)
            except Exception:
                size = 0
        else:
            # minimal fallback from metadata
            try:
                for f in (post.get("metadata") or {}).get("files") or []:
                    if isinstance(f, dict) and str(f.get("id")) == fid:
                        filename = f.get("name") or ""
                        mime = f.get("mime_type") or ""
                        try: size = int(f.get("size") or 0)
                        except: size = 0
                        break
            except Exception:
                pass
            filename = filename or fid
        # size guard: 500MB 한도까지 저장·활용하되 500MB 원본을 LLM에 직전송하지 않는다.
        if size and size > MAX_TOTAL_ATTACHMENT_BYTES:
            ref = {
                "file_id": fid,
                "attachment_id": fid,
                "kind": "stored_only",
                "vault_path": _owner_vault_fallback(tenant_id, agent, fid, filename or fid),
                "filename": _sanitize_filename(filename or fid),
                "mime_type": (mime or "").strip().lower().split(";")[0].strip() or "unknown",
                "size": size,
                "source": "mattermost",
                "reason": "over_limit",
                "stored": False,
                "extractable": _is_extractable_hint(mime, filename or ""),
                "extract_hint": (pathlib.Path(filename or "").suffix.lower().lstrip(".") or "over_limit"),
                "extracted_text": None,
            }
            print(f"[attach] over 500m fid={fid[:6]} size={size} — metadata only", flush=True)
            file_ids.append(fid)
            refs.append(ref)
            continue
        if not _is_image_mime(mime, filename):
            # IMAGE / TEXT_PREVIEW / STORED_ONLY 라우터 (바이트는 IMAGE만, 미리보기는 마스킹 텍스트만)
            # 원본은 owner vault에 스트리밍 보존, LLM에는 메타/마스킹 미리보기만 전달
            ref = _build_non_image_ref(fid, filename or fid, mime, size, tenant_id, agent)
            # Never log raw filenames: they may contain secret-bearing names.
            print(f"[attach] routed fid={fid[:6]} kind={ref.get('kind')} mime={mime or 'unknown'} sensitive={bool(ref.get('reason') == 'sensitive')}", flush=True)
            file_ids.append(fid)
            refs.append(ref)
            continue
        safe_name = _sanitize_filename(filename or f"{fid}.png")
        mime_norm = _normalize_mime(mime or "image/png")
        if not _is_allowed_image_mime(mime_norm):
            # MIME says non-image but extension looked like an image: do not
            # silently drop the file (file_ids would vanish → CP 400 on
            # file-only posts, bytes never preserved). Route through the
            # non-image router so bytes are vault-stored and a metadata ref
            # is still forwarded.
            print(f"[attach] disallowed image mime fid={fid[:6]} mime={mime_norm} — routing as stored_only", flush=True)
            ref = _build_non_image_ref(fid, filename or fid, mime, size, tenant_id, agent)
            print(f"[attach] routed fid={fid[:6]} kind={ref.get('kind')} mime={mime or 'unknown'} sensitive={bool(ref.get('reason') == 'sensitive')}", flush=True)
            file_ids.append(fid)
            refs.append(ref)
            continue
        # safe vault reference (no secret, no FS traversal) — owner-scoped relative path.
        # Control Plane / ACP resolves via active runtime; absolute paths and MM
        # API URLs are never emitted (CP strips them; the gate needs data_url only).
        vault_path = _owner_vault_fallback(tenant_id, agent, fid, safe_name)
        ref = {
            "file_id": fid,
            "attachment_id": fid,
            "kind": "image",
            "vault_path": vault_path,
            "filename": safe_name,
            "mime_type": mime_norm,
            "size": size,
            "source": "mattermost",
            "stored": False,
            "extractable": False,
            "extract_hint": "image",
            "extracted_text": None,
        }
        # ── Durable owner-vault store (streaming Bearer 64KB, 500MB cap) — decoupled from LLM bytes ──
        # Original image bytes are preserved in the owner vault even though only a
        # bounded 20MB data_url (below) is ever sent to the LLM.
        if agent:
            try:
                _vmeta = _persist_attachment_to_vault(fid, filename or safe_name, tenant_id, agent)
                if isinstance(_vmeta, dict) and _vmeta.get("stored"):
                    ref["vault_path"] = str(_vmeta.get("vault_path") or vault_path)
                    ref["stored"] = True
                    ref["sha256"] = str(_vmeta.get("sha256") or "")
                    # actual durable size; LLM data_url below stays bounded at 20MB
                    ref["size"] = int(_vmeta.get("size") or size or 0)
                elif isinstance(_vmeta, dict) and _vmeta.get("over_limit"):
                    # stream proved >500MB: metadata-only, no 2nd download bypass
                    ref["vault_path"] = str(_vmeta.get("vault_path") or vault_path)
                    ref["stored"] = False
                    ref["reason"] = "over_limit"
                    try:
                        ref["size"] = int(_vmeta.get("size") or size or 0)
                    except Exception:
                        pass
                    print(f"[attach] image over 500m cap fid={fid[:6]} — metadata only, no bytes", flush=True)
                    file_ids.append(fid)
                    refs.append(ref)
                    continue
            except Exception:
                pass
        # Vault-proven oversize for the LLM path: skip the 2nd 20MB download.
        try:
            _vsize = int(ref.get("size") or 0)
        except Exception:
            _vsize = 0
        if ref.get("stored") and _vsize > MAX_ATTACHMENT_BYTES:
            print(f"[attach] image bytes skipped (vault {_vsize}B > 20MB LLM bound) fid={fid[:6]}", flush=True)
            file_ids.append(fid)
            refs.append(ref)
            continue
        # ── Bounded base64/data URL delivery (vault-first, single-download) ──
        # Durable stream already fetched the bytes: reuse the local vault copy
        # (bounded 20MB+1 read) instead of a 2nd authenticated MM download.
        # HTTP fallback only when vault missed/failed. MIME allowlist enforced below.
        dl_bytes: bytes | None = None
        dl_mime: str | None = None
        if ref.get("stored"):
            try:
                _ivb = _read_vault_prefix_bytes(str(ref.get("vault_path") or ""), MAX_ATTACHMENT_BYTES)
            except Exception:
                _ivb = None
            if isinstance(_ivb, bytes) and _ivb:
                if len(_ivb) > MAX_ATTACHMENT_BYTES:
                    print(f"[attach] skip oversized vault bytes fid={fid[:6]} {len(_ivb)}", flush=True)
                    file_ids.append(fid)
                    refs.append(ref)
                    continue
                dl_bytes, dl_mime = _ivb, mime_norm
        if dl_bytes is None:
            dl_bytes, dl_mime = _download_mattermost_file_bytes(fid)
        if dl_bytes is not None:
            effective_mime = _normalize_mime(dl_mime or mime_norm)
            if not _is_allowed_image_mime(effective_mime):
                print(f"[attach] skip non-image download mime fid={fid[:6]} mime={effective_mime}", flush=True)
            elif len(dl_bytes) > MAX_ATTACHMENT_BYTES:
                print(f"[attach] skip oversized bytes fid={fid[:6]} {len(dl_bytes)}", flush=True)
            else:
                # LLM transfer size (bounded 20MB data_url). Durable vault size stored
                # above is canonical — only fill size from download when vault missed.
                if not ref.get("stored"):
                    ref["size"] = len(dl_bytes)
                # keep mime consistent with actual content-type if provided
                if dl_mime:
                    ref["mime_type"] = effective_mime
                    mime_norm = effective_mime
                # bounded base64 data URL for Hermes runtime (OpenAI multimodal image_url)
                try:
                    b64 = base64.b64encode(dl_bytes).decode("ascii")
                    data_url = f"data:{mime_norm};base64,{b64}"
                    # optional: guard encoded size still reasonable (base64 ~1.33x)
                    ref["data_url"] = data_url
                    ref["base64"] = b64  # alias for ACP compatibility
                except Exception as e:
                    print(f"[attach] base64 encode failed fid={fid[:6]} err={e}", flush=True)
        else:
            # download failed -> keep ref without data_url; ACP will fallback to file:// (still preserves context)
            # but log for observability; don't fabricate bytes
            print(f"[attach] no bytes for fid={fid[:6]} — forwarding ref without data_url (ACP may fallback)", flush=True)
        file_ids.append(fid)
        refs.append(ref)
    return file_ids, refs

def api_get(path):
    url = f"{MATTERMOST_URL}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {BOT_TOKEN}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

def api_post(path, body):
    url = f"{MATTERMOST_URL}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Authorization": f"Bearer {BOT_TOKEN}", "Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

def cp_post(path, body):
    url = f"{CONTROL_PLANE}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json"})
    req.add_header("X-Mattermost-Signature", _signature(data))
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _http_error_status(exc):
    return getattr(exc, "code", None)


def _is_permanent_cp_error(exc):
    return _http_error_status(exc) in {400, 401, 403, 404, 405, 409, 422}


def _cp_error_detail(exc):
    """Read a bounded, non-secret Control Plane detail for classification only."""
    try:
        raw = exc.read(4096).decode("utf-8", errors="replace")
        try:
            value = json.loads(raw)
            detail = value.get("detail", value.get("message", "")) if isinstance(value, dict) else ""
        except json.JSONDecodeError:
            detail = raw
        return re.sub(r"[\\r\\n]+", " ", str(detail)).strip()[:240]
    except Exception:
        return ""


def _cp_user_message(status, detail):
    """Map CP errors to safe Korean UX text; never expose internal error bodies."""
    normalized = (detail or "").lower()
    if status == 403 and any(token in normalized for token in ("registration", "registered", "onboarding", "mapping")):
        return "현재 OAOS 사용자 등록이 되어 있지 않습니다. 웹관리자 콘솔에서 등록 상태를 확인한 뒤 다시 시도해 주세요."
    if status == 400:
        if "text/message required" in normalized:
            return ("첨부 파일 형식 안내: 이미지는 바로 확인할 수 있고, 텍스트·코드·CSV 등은 "
                    "미리보기(최대 200KB, 민감 정보 제외)로 확인합니다. 그 외 형식은 이미지 또는 "
                    "텍스트로 다시 보내 주세요. 문제가 계속되면 관리자에게 문의해 주세요.")
        return "요청 형식을 확인해 주세요. 문제가 계속되면 관리자에게 문의해 주세요."
    if status == 401:
        return "OAOS 인증 확인에 실패했습니다. 관리자에게 문의해 주세요."
    if status == 403:
        return "현재 계정에는 이 작업을 수행할 권한이 없습니다."
    if status == 404:
        return "요청한 세션 또는 리소스를 찾을 수 없습니다. 새 대화로 다시 시도해 주세요."
    if status == 405:
        return "지원되지 않는 요청입니다. 관리자에게 문의해 주세요."
    if status == 409:
        return "이미 처리 중이거나 처리된 요청입니다. 잠시 후 확인해 주세요."
    if status == 422:
        return "입력값을 확인해 주세요."
    if status == 408:
        return "처리 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요."
    if status == 429:
        return "현재 요청이 많습니다. 잠시 후 다시 시도해 주세요."
    if status in {500, 502, 503, 504}:
        return "OAOS 연동 서비스가 일시적으로 응답하지 않습니다. 잠시 후 다시 시도해 주세요."
    return "OAOS 요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요."


def _post_registration_notice(channel_id, root_id, message):
    """Post one non-LLM notice for a permanent rejection."""
    try:
        api_post("/api/v4/posts", {
            "channel_id": channel_id,
            "root_id": root_id,
            "message": message,
        })
        print(f"[cp] user notice posted root={root_id[:6]}", flush=True)
    except Exception as exc:
        print(f"[cp] user notice failed root={root_id[:6]} err={str(exc)[:120]}", flush=True)


def _typing_loop(channel_id, stop_event):
    """Keep Mattermost's native status indicator alive while processing."""
    while not stop_event.is_set():
        try:
            api_post(f"/api/v4/users/{BOT_ID}/typing", {"channel_id": channel_id})
        except Exception as e:
            print(f"[typing] failed: {e}", flush=True)
        stop_event.wait(4.0)

def get_user(user_id):
    try:
        return api_get(f"/api/v4/users/{user_id}")
    except Exception:
        return {}

def get_username(user_id):
    return get_user(user_id).get("username", "")

def resolve_employee(username, user_id):
    raw = (username or user_id or "unknown").lower()
    suffix = re.sub(r"[^a-z0-9_.-]", "", raw) or "unknown"
    return f"employee:{suffix}", f"agent:assistant:{suffix}"

def load_seen():
    if SEEN_FILE.exists():
        try: return set(json.loads(SEEN_FILE.read_text()))
        except: return set()
    return set()

def save_seen(s):
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(list(s)[-500:]))


def _confirm_reply_background(channel_id, bot_id, thread_root, post_id, source_create_at, typing_stop):
    """Confirm the final reply without blocking the main Mattermost poller."""
    try:
        confirmed = wait_for_bot_reply(channel_id, bot_id, thread_root, post_id, source_create_at)
        if confirmed:
            print(f"[cp] confirmed bot reply {confirmed[:6]} root={thread_root[:6]}", flush=True)
        else:
            print(f"[cp] reply timeout after {POST_CONFIRM_TIMEOUT_S}s post={post_id[:6]} root={thread_root[:6]}", flush=True)
    except Exception as e:
        print(f"[cp] reply confirmation failed {e}", flush=True)
    finally:
        typing_stop.set()
        print(f"[typing] stopped channel={channel_id[:6]}", flush=True)


def call_ollama_fallback(username, employee, msg):
    sys_prompt = (
        f"You are Open Agent OS personal agent @agent for {username} ({employee}). "
        "You are SEPARATE from Hermes @openit (company-wide CoCo). Reply in Korean, concise, helpful. "
        f"Identity {employee} <-> agent:assistant:{username}."
    )
    body = {"model": OLLAMA_MODEL, "messages": [{"role":"system","content":sys_prompt},{"role":"user","content":msg}], "stream": False, "think": False}
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=data, headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode())
            return ((resp.get("message")or{}).get("content") or "").strip()
    except Exception as e:
        print(f"[ollama fallback] {e}", flush=True)
        return ""

def poll_once(seen, tenant_id: str | None = None):
    _tenant_default = _resolve_bridge_tenant(tenant_id)
    new_seen = set(seen)
    try:
        channels = api_get("/api/v4/users/me/channels")
    except Exception as e:
        print(f"[poll] channels failed: {e}", flush=True)
        return new_seen, 0
    dms = [c for c in channels if c.get("type")=="D"]
    replied = 0
    channel_posts = fetch_channel_posts_parallel(dms)
    for ch in dms:
        cid = ch["id"]
        data = channel_posts.get(cid)
        if not data:
            continue
        order = data.get("order", [])
        posts = data.get("posts", {})
        for pid in reversed(order):
            if pid in new_seen:
                continue
            p = posts.get(pid,{})
            # Commit the source post to seen only after forwarding succeeds;
            # transient Control Plane failures must be retried, not lost.
            uid = p.get("user_id","")
            msg = (p.get("message") or "").strip()
            # Attachment-aware skip: allow image-only posts (no text) to flow through runtime
            _pre_file_ids = p.get("file_ids") if isinstance(p.get("file_ids"), list) else []
            _has_attach = bool(_pre_file_ids)
            if not _has_attach:
                try:
                    _mf = (p.get("metadata") or {}).get("files") if isinstance(p.get("metadata"), dict) else None
                    if isinstance(_mf, list) and _mf:
                        _has_attach = True
                except Exception:
                    pass
            if (not msg and not _has_attach) or uid == BOT_ID: continue
            if p.get("type") not in ("", None): continue
            user_record = get_user(uid)
            # Only human-originated posts enter the personal-agent path.
            # @openit is Hermes system management and any other bot is excluded.
            if user_record.get("is_bot") is True:
                print(f"[skip] bot origin uid={uid[:6]} username={user_record.get('username','')}", flush=True)
                new_seen.add(pid)
                continue
            username = user_record.get("username", "")
            employee, agent = resolve_employee(username, uid)
            print(f"[new] {username}({uid[:6]}) -> {employee} : {msg[:80]}", flush=True)
            # Make processing visible to the user immediately. This is a bot reply
            # in the same thread; the bridge ignores bot-originated posts.
            thread_root = p.get("root_id") or pid
            typing_stop = threading.Event()
            typing_thread = threading.Thread(target=_typing_loop, args=(cid, typing_stop), daemon=True)
            typing_thread.start()
            # Build attachment refs: IMAGE / TEXT_PREVIEW / STORED_ONLY (500MB 한도, 비밀원문 미전송)
            # Owner context (tenant/employee/agent) scopes the durable Vault path per ref.
            # Tenant matches the CP server authority so CP owner validation passes.
            _tenant = _tenant_default
            file_ids, attachment_refs = build_attachment_refs_for_post(
                p, tenant_id=_tenant, employee=employee, agent=agent,
            )
            if file_ids:
                kinds = {}
                for r in attachment_refs:
                    k = (r.get("kind") or "image") if isinstance(r, dict) else "image"
                    kinds[k] = kinds.get(k, 0) + 1
                print(f"[attach] forwarding {len(file_ids)} file(s) {kinds} fid={file_ids[0][:6]}", flush=True)
            # 문서 미리보기·저장 참조를 CP 텍스트에 합성 (500MB 원본 직전송 없음, 마스킹 텍스트만)
            # CP-side bounded extractor가 채운 extracted_text가 있으면 함께 합성 (bridge는 추출 안 함)
            effective_text = msg
            try:
                _blocks = []
                for r in attachment_refs or []:
                    if not isinstance(r, dict):
                        continue
                    _ext = r.get("extracted_text") if isinstance(r.get("extracted_text"), str) else ""
                    if r.get("kind") == "text_preview" and r.get("preview"):
                        _blocks.append(_preview_block(r))
                    elif (r.get("kind") or "image") != "image":
                        _blocks.append(_stored_note(r))
                    if _ext and _ext.strip():
                        _bounded = _mask_secrets(_ext.strip()[:20000])
                        _fname = _mask_secrets(str(r.get("filename") or "첨부 파일"))
                        _blocks.append(f"[첨부 추출 텍스트: {_fname}]\n{_bounded}")
                if _blocks:
                    _note = "\n\n".join(_blocks)
                    effective_text = f"{msg}\n\n{_note}".strip() if msg else _note
                    if len(effective_text) > 220000:
                        effective_text = effective_text[:220000] + "\n[…미리보기 truncated…]"
            except Exception:
                effective_text = msg
            runtime_context = {
                "platform": "mattermost",
                "tenant_id": _tenant,
                "user_id": employee,
                "agent_id": agent,
                "channel_id": cid,
                "post_id": pid,
                "root_id": thread_root,
            }
            # Forward to Control Plane standard endpoint (thread root correctly)
            payload = {
                "tenant_id": _tenant,
                "user_id": employee,
                "agent_id": agent,
                "text": effective_text,
                "channel_id": cid,
                "post_id": pid,
                "root_id": thread_root,
                "file_ids": file_ids,
                "attachment_refs": attachment_refs,
                "runtime_context": runtime_context,
            }
            # Also include single attachment_ref alias for CP compatibility (first image)
            if attachment_refs:
                payload["attachment_ref"] = attachment_refs[0]
            try:
                res = cp_post("/v1/mattermost/events", payload)
                print(f"[cp] {res.get('session_id','')[:8]} acp={res.get('acp',{}).get('status','')} routed={res.get('routed','')} registration={res.get('registration_state','')}", flush=True)
                # Onboarding responses are returned synchronously by the Control
                # Plane and must be posted by @agent in the same DM thread.
                # They are not forwarded to Hermes and contain no Google data.
                registration_message = (res.get("message") or "").strip()
                if res.get("registration_gate") and registration_message:
                    api_post("/api/v4/posts", {"channel_id": cid, "root_id": thread_root, "message": registration_message})
                # Mark seen immediately after successful forward to prevent duplicate storm.
                # Transient CP failures (exception) will NOT be marked, so they retry.
                new_seen.add(pid)
                # Gateway LLM may take ~28s; poll for the exact thread reply.
                # Pass source timestamp (or full source post) so stale bot replies are not confirmed.
                try:
                    source_create_at = int(p.get("create_at") or 0)
                except Exception:
                    source_create_at = 0
                # Prefer passing timestamp; also supports passing full source post dict
                # Reply confirmation is observability only; never block polling.
                threading.Thread(
                    target=_confirm_reply_background,
                    args=(cid, BOT_ID, thread_root, pid, source_create_at if source_create_at else p, typing_stop),
                    daemon=True,
                ).start()
            except Exception as e:
                status = _http_error_status(e)
                if _is_permanent_cp_error(e):
                    # Permanent policy/registration errors must not be retried or
                    # forwarded to any LLM. Mark seen and issue at most one notice.
                    new_seen.add(pid)
                    detail = _cp_error_detail(e)
                    user_message = _cp_user_message(status, detail)
                    _post_registration_notice(cid, thread_root, user_message)
                    print(f"[cp] permanent rejection status={status} post={pid[:6]} detail={detail[:120]!r} — no retry/no LLM", flush=True)
                else:
                    print(f"[cp] transient failure status={status} err={e} — retry eligible", flush=True)
                typing_stop.set()
            finally:
                # The typing loop is independently bounded and stopped by the
                # background confirmation worker when the reply arrives.
                pass
    return new_seen, replied

def main():
    global BOT_ID
    if not BOT_ID:
        try:
            me = api_get("/api/v4/users/me")
            BOT_ID = str(me.get("id") or "")
        except Exception as e:
            print(f"[init] cannot resolve bot id: {e} — refusing to start (fail-closed)", flush=True)
            raise SystemExit(1)
    if not BOT_ID:
        print("[init] empty bot id — refusing to start (fail-closed)", flush=True)
        raise SystemExit(1)
    print(f"[oaos-mm-bridge] MATTERMOST={MATTERMOST_URL} BOT={BOT_ID[:6]} CP={CONTROL_PLANE} OLLAMA={OLLAMA_MODEL}", flush=True)
    _bridge_tenant = _resolve_bridge_tenant()
    print(f"[oaos-mm-bridge] tenant={_bridge_tenant}", flush=True)
    seen = load_seen()
    if not SEEN_FILE.exists():
        try:
            channels = api_get("/api/v4/users/me/channels")
            for ch in [c for c in channels if c.get("type")=="D"]:
                data = api_get(f"/api/v4/channels/{ch['id']}/posts?page=0&per_page=50")
                for pid in data.get("order",[]): seen.add(pid)
            save_seen(seen)
            print(f"[init] seeded {len(seen)}", flush=True)
        except Exception as e: print(f"[init] {e}", flush=True)
    while True:
        try:
            prev_len = len(seen)
            seen, n = poll_once(seen, tenant_id=_bridge_tenant)
            # Persist whenever seen grows (not only when reply confirmed) to prevent re-processing same post
            if len(seen) != prev_len:
                save_seen(seen)
            elif n:
                save_seen(seen)
        except Exception as e: print(f"[loop] {e}", flush=True)
        time.sleep(2)

if __name__ == "__main__": main()
