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
BOT_ID = "bmhbteup4p8bmb8rfh151y6w1e"
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
    # safe vault/local filename: keep alnum, dot, dash, underscore
    base = pathlib.Path(name or "attachment").name
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "attachment"
    # avoid path traversal
    safe = safe.replace("..", "_")
    return safe[:180]

def get_mattermost_file_info(file_id: str) -> dict:
    """Fetch Mattermost file metadata safely (no secret logging). Returns normalized dict or {} on failure."""
    fid = (file_id or "").strip()
    if not fid or not re.match(r"^[a-z0-9]+$", fid):
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
    if not fid or not re.match(r"^[a-z0-9]+$", fid):
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

def build_attachment_refs_for_post(post: dict) -> tuple[list[str], list[dict]]:
    """Build file_ids + attachment_refs for image attachments on a post.

    - Uses post file_ids and optionally metadata.files
    - Fetches file info via api_get for mime/size validation
    - Filters to image/* (by mime or extension) — non-images ignored for this slice
    - No OCR, no model/provider selection — only a safe vault/local reference + authenticated URL
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
        # size guard: skip absurdly large files (log, skip rather than OOM)
        if size and size > MAX_ATTACHMENT_BYTES:
            print(f"[attach] skip oversized fid={fid[:6]} size={size}", flush=True)
            continue
        if not _is_image_mime(mime, filename):
            # narrow slice: only forward images; skip non-images silently (trace)
            print(f"[attach] skip non-image fid={fid[:6]} mime={mime or 'unknown'} name={filename[:40]}", flush=True)
            continue
        safe_name = _sanitize_filename(filename or f"{fid}.png")
        mime_norm = _normalize_mime(mime or "image/png")
        if not _is_allowed_image_mime(mime_norm):
            print(f"[attach] skip disallowed mime fid={fid[:6]} mime={mime_norm}", flush=True)
            continue
        # safe vault reference (no secret, no FS traversal) — Control Plane / ACP resolves via active runtime
        vault_path = f"mattermost/{fid}/{safe_name}"
        # authenticated reference URL (requires Bearer — not fetched here)
        source_url = f"{MATTERMOST_URL}/api/v4/files/{fid}"
        # local cache reference (not auto-downloaded; path reserved for optional lazy fetch)
        try:
            ATTACH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        local_path = str(ATTACH_CACHE_DIR / f"{fid}_{safe_name}")
        ref = {
            "file_id": fid,
            "attachment_id": fid,
            "vault_path": vault_path,
            "filename": safe_name,
            "mime_type": mime_norm,
            "size": size,
            "source": "mattermost",
            "url": source_url,
            "local_path": local_path,
        }
        # ── Bounded base64/data URL delivery (authenticated download) ──
        # Download bytes via Bridge's authenticated MM API; MIME validation + max-size enforced inside download
        dl_bytes, dl_mime = _download_mattermost_file_bytes(fid)
        if dl_bytes is not None:
            effective_mime = _normalize_mime(dl_mime or mime_norm)
            if not _is_allowed_image_mime(effective_mime):
                print(f"[attach] skip non-image download mime fid={fid[:6]} mime={effective_mime}", flush=True)
            elif len(dl_bytes) > MAX_ATTACHMENT_BYTES:
                print(f"[attach] skip oversized bytes fid={fid[:6]} {len(dl_bytes)}", flush=True)
            else:
                # update size to actual bytes if info size mismatched
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

def poll_once(seen):
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
            # Build image attachment refs (no OCR, no model/provider selection) for Agent Runtime
            file_ids, attachment_refs = build_attachment_refs_for_post(p)
            if file_ids:
                print(f"[attach] forwarding {len(file_ids)} image(s) fid={file_ids[0][:6]}", flush=True)
            runtime_context = {
                "platform": "mattermost",
                "tenant_id": "default",
                "user_id": employee,
                "channel_id": cid,
                "post_id": pid,
                "root_id": thread_root,
            }
            # Forward to Control Plane standard endpoint (thread root correctly)
            payload = {
                "tenant_id": "default",
                "user_id": employee,
                "text": msg,
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
    print(f"[oaos-mm-bridge] MATTERMOST={MATTERMOST_URL} BOT={BOT_ID[:6]} CP={CONTROL_PLANE} OLLAMA={OLLAMA_MODEL}", flush=True)
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
            seen, n = poll_once(seen)
            # Persist whenever seen grows (not only when reply confirmed) to prevent re-processing same post
            if len(seen) != prev_len:
                save_seen(seen)
            elif n:
                save_seen(seen)
        except Exception as e: print(f"[loop] {e}", flush=True)
        time.sleep(2)

if __name__ == "__main__": main()
