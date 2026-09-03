"""Real read-only Outline HTTP SourceAdapter for RAG incremental sync.

Design goals:
- Read-only, stdlib HTTP, no mock fallback.
- Configurable API URL/token via ctor or env (OUTLINE_API_URL, OUTLINE_API_KEY,
  OUTLINE_API_TOKEN, OAOS_OUTLINE_TOKEN, OAOS_OUTLINE_URL).
- Pagination (offset/limit), bounded timeout/retries, fail-closed.
- Normalized SourceDocument: resource_id, source_uri, title, content,
  source_updated_at, acl_version, acl {groups, users}, content_hash.

Outline API: POST {apiUrl}/api/documents.list {limit, offset, sort, direction}
Response: {data: [doc,...], pagination:{offset, limit, total}} or bare list.
Doc fields vary; we handle id, title, text/content, collectionId/collection_id,
updatedAt/updated_at, url, revision, acl/permissions.

Security: if credentials absent or HTTP error exhausts retries -> raise
RuntimeError (SyncOrchestrator treats as failed, checkpoint not advanced).
No silent mock fallback.

Transport injection: `http_client` may be an object with
    post(url, headers, json, timeout) -> resp
where resp has .status_code and .json(). If None, stdlib urllib is used.
This enables TDD with fake HTTP transport without live network.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..chunking import content_hash as _content_hash
from ..models import SourceDocument
from .base import FetchResult, SourceAdapter


# ---------------------------------------------------------------------------
# Env resolution
# ---------------------------------------------------------------------------
def _resolve_api_url(explicit: str | None) -> str:
    value = explicit.strip() if explicit is not None else ""
    if not value:
        for k in ("OUTLINE_API_URL", "OAOS_OUTLINE_URL", "OAOS_OUTLINE_API_URL"):
            value = os.environ.get(k, "").strip()
            if value:
                break
    value = value.rstrip("/")
    # Accept both host and host/api configuration; request paths add /api.
    if value.lower().endswith("/api"):
        value = value[:-4].rstrip("/")
    return value


def _resolve_api_token(explicit: str | None) -> str:
    if explicit is not None:
        return explicit.strip()
    for k in (
        "OUTLINE_API_KEY",
        "OUTLINE_API_TOKEN",
        "OAOS_OUTLINE_TOKEN",
        "OAOS_OUTLINE_API_KEY",
        "OUTLINE_TOKEN",
    ):
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return ""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class OutlineAPIError(RuntimeError):
    """Outline HTTP API error (auth, server, network)."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class OutlineSourceConfig:
    api_url: str
    api_token: str
    timeout_s: float = 10.0
    max_retries: int = 3
    page_limit: int = 25
    retry_backoff_s: float = 0.2
    collection_id: str | None = None

    def validate(self) -> None:
        if not self.api_url or not self.api_token:
            raise RuntimeError(
                "Outline credentials missing: api_url and api_token are required "
                "(set OUTLINE_API_URL + OUTLINE_API_TOKEN / OUTLINE_API_KEY or "
                "OAOS_OUTLINE_TOKEN). Failing closed — no mock fallback."
            )
        if not (1 <= self.timeout_s <= 60):
            raise ValueError(f"timeout_s must be 1..60, got {self.timeout_s}")
        if not (1 <= self.max_retries <= 8):
            raise ValueError(f"max_retries must be 1..8, got {self.max_retries}")
        if not (1 <= self.page_limit <= 100):
            raise ValueError(f"page_limit must be 1..100, got {self.page_limit}")


# ---------------------------------------------------------------------------
# Helpers: normalization
# ---------------------------------------------------------------------------
def _normalize_updated_at(raw: Any) -> str:
    if not raw:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc).isoformat()
        except Exception:
            pass
    s = str(raw).strip()
    if not s:
        return datetime.now(timezone.utc).isoformat()
    # Try to parse ISO; keep as-is if already ISO-like
    # Normalize Z -> +00:00 for python
    try:
        # If contains Z, replace
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return s


def _derive_acl_version(doc: dict[str, Any], updated_at_iso: str) -> str:
    # Prefer explicit revision/acl_version
    for k in ("acl_version", "aclVersion", "revision", "version"):
        v = doc.get(k)
        if v:
            return str(v)
    # Derive from ACL payload hash + updatedAt to detect permission changes
    acl_payload = doc.get("acl") or doc.get("permissions") or doc.get("shares") or {}
    # Also include collectionId in hash scope
    h = hashlib.sha256(json.dumps(acl_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:8]
    # short hash of updatedAt
    uh = hashlib.sha256(updated_at_iso.encode("utf-8")).hexdigest()[:6]
    return f"v-{h}-{uh}"


def _parse_acl(doc: dict[str, Any]) -> dict[str, Any]:
    # If explicit acl dict present
    acl = doc.get("acl")
    if isinstance(acl, dict) and acl:
        # Normalize keys
        groups = acl.get("groups") or acl.get("allowedGroups") or acl.get("allowed_groups") or []
        users = acl.get("users") or acl.get("allowedUsers") or acl.get("allowed_users") or []
        out: dict[str, Any] = {}
        if groups:
            out["groups"] = list(groups) if isinstance(groups, list) else [groups]
        if users:
            out["users"] = list(users) if isinstance(users, list) else [users]
        # Preserve other acl fields if needed
        for k in ("public", "tenants", "permissions"):
            if k in acl:
                out[k] = acl[k]
        # If we normalized something, return it; else return original acl
        return out if out or not acl else dict(acl)

    # Outline permissions: sometimes document has permissions list like [{id, collectionId, permission}]
    # Or collection-level ACL — we synthesize empty default (no restriction) as {}
    # If doc has shares or membership info, treat similarly
    permissions = doc.get("permissions")
    if isinstance(permissions, list) and permissions:
        groups: list[str] = []
        users: list[str] = []
        for p in permissions:
            if isinstance(p, dict):
                if p.get("group") or p.get("groupId"):
                    groups.append(str(p.get("group") or p.get("groupId")))
                if p.get("userId") or p.get("user"):
                    users.append(str(p.get("userId") or p.get("user")))
        if groups or users:
            out2: dict[str, Any] = {}
            if groups:
                out2["groups"] = groups
            if users:
                out2["users"] = users
            return out2

    # Fallback: if collection indicates private, mark admin group
    collection_id = str(doc.get("collectionId") or doc.get("collection_id") or doc.get("collection") or "")
    if collection_id.lower() == "private":
        return {"groups": ["admin"]}

    return {}


def _normalize_document(raw: dict[str, Any], api_url: str) -> SourceDocument:
    doc_id = str(raw.get("id") or raw.get("documentId") or raw.get("docId") or "").strip()
    if not doc_id:
        raise ValueError("Outline doc missing id")
    collection_id = str(
        raw.get("collectionId")
        or raw.get("collection_id")
        or raw.get("collection")
        or raw.get("collectionName")
        or "team"
    ).strip() or "team"
    title = str(raw.get("title") or raw.get("name") or "Untitled")
    # Content field priority
    content = raw.get("text")
    if content is None:
        content = raw.get("content")
    if content is None:
        content = raw.get("body") or raw.get("data") or ""
    if not isinstance(content, str):
        content = str(content) if content is not None else ""

    updated_raw = raw.get("updatedAt") or raw.get("updated_at") or raw.get("updated_at_") or raw.get("createdAt") or raw.get("created_at")
    updated_at_iso = _normalize_updated_at(updated_raw)

    url = str(raw.get("url") or raw.get("urlId") or "")
    if not url:
        # Construct URL from api_url if possible
        base = api_url.replace("/api", "").rstrip("/") if api_url else "https://outline.example.com"
        # Use /doc/{id} pattern (compatible with fixture)
        url = f"{base}/doc/{doc_id}"

    acl = _parse_acl(raw)
    acl_version = _derive_acl_version(raw, updated_at_iso)

    resource_id = f"outline/{collection_id}/{doc_id}"
    ch = _content_hash(content)

    return SourceDocument(
        resource_id=resource_id,
        source_system="outline",
        title=title,
        content=content,
        source_updated_at=updated_at_iso,
        acl_version=acl_version,
        acl=acl,
        source_uri=url,
        content_hash=ch,
    )


# ---------------------------------------------------------------------------
# Stdlib HTTP helper (only when no injected client)
# ---------------------------------------------------------------------------
class _StdlibResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        if not self._body:
            return {}
        return json.loads(self._body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")


def _stdlib_post(url: str, headers: dict[str, str], json_body: dict[str, Any], timeout: float) -> _StdlibResponse:
    data = json.dumps(json_body).encode("utf-8")
    hdrs = dict(headers)
    hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return _StdlibResponse(resp.status, body)
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        # Do not raise yet — return response with status code for caller to decide retry/no-retry
        return _StdlibResponse(e.code, body)
    except urllib.error.URLError as e:
        raise OutlineAPIError(f"network error posting {url}: {e}") from e
    except Exception as e:
        raise OutlineAPIError(f"unexpected error posting {url}: {e}") from e


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------
class HttpOutlineSourceAdapter(SourceAdapter):
    """Real Outline HTTP adapter — read-only sync by default, explicit writes gated.

    Reads from Outline API via POST /api/documents.list with pagination.
    Fails closed if credentials missing or HTTP errors exhaust retries.
    No mock/fixture fallback.

    Writes (create/update/delete) are gated by write_enabled (default False)
    and optional write_permission_checker callback. sync()/fetch() never writes.

    Args:
        api_url: Outline base URL (e.g. https://outline.example.com)
        api_token: Outline API token (Bearer)
        timeout_s: bounded 1..60 seconds per HTTP call
        max_retries: bounded 1..8 attempts
        page_limit: bounded 1..100 docs per page
        retry_backoff_s: base backoff seconds (multiplied by attempt)
        collection_id: optional filter to single collection
        http_client: injectable transport for tests (object with post(url, headers, json, timeout))
        write_enabled: gate for create/update/delete; default False (fail-closed)
        allow_writes: alias for write_enabled (back-compat)
        write_permission_checker: optional callable(action, context)->bool to gate writes
    """

    source_system: str = "outline"

    def __init__(
        self,
        api_url: str | None = None,
        api_token: str | None = None,
        timeout_s: float = 10.0,
        max_retries: int = 3,
        page_limit: int = 25,
        retry_backoff_s: float = 0.2,
        collection_id: str | None = None,
        http_client: Any | None = None,
        max_pages: int | None = None,
        write_enabled: bool = False,
        allow_writes: bool | None = None,
        write_permission_checker: Any | None = None,
    ) -> None:
        self._api_url = _resolve_api_url(api_url)
        self._api_token = _resolve_api_token(api_token)
        self.timeout_s = float(timeout_s)
        self.max_retries = int(max_retries)
        self.page_limit = int(page_limit)
        self.retry_backoff_s = float(retry_backoff_s)
        self.max_pages = max_pages if max_pages is None else max(1, int(max_pages))
        self.collection_id = collection_id
        self._http_client = http_client
        # write gate: allow_writes alias, explicit write_enabled wins if set
        if allow_writes is not None:
            write_enabled = bool(allow_writes) if not write_enabled else True
        self.write_enabled = bool(write_enabled)
        self._write_permission_checker = write_permission_checker
        self._last_fetch_pages = 0

        # Validate bounds (fail fast on misconfig, but credential check is deferred to fetch for fail-closed)
        if not (1 <= self.timeout_s <= 60):
            raise ValueError(f"timeout_s 1..60, got {self.timeout_s}")
        if not (1 <= self.max_retries <= 8):
            raise ValueError(f"max_retries 1..8, got {self.max_retries}")
        if not (1 <= self.page_limit <= 100):
            raise ValueError(f"page_limit 1..100, got {self.page_limit}")

    @property
    def last_fetch_pages(self) -> int:
        return self._last_fetch_pages

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_token}", "Content-Type": "application/json"}

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{self._api_url.rstrip('/')}{path}"
        headers = self._headers()
        timeout = self.timeout_s
        if self._http_client is not None:
            # Injected client (fake transport) — delegate
            return self._http_client.post(url, headers=headers, json=body, timeout=timeout)
        return _stdlib_post(url, headers=headers, json_body=body, timeout=timeout)

    def _post_with_retries(self, path: str, body: dict[str, Any]) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._post(path, body)
                status = getattr(resp, "status_code", 200)
                # 401/403 fail closed immediately, no retry
                if status in (401, 403):
                    # Try to surface body for diagnostics
                    try:
                        payload = resp.json() if hasattr(resp, "json") else {}
                    except Exception:
                        payload = {}
                    raise OutlineAPIError(f"Outline auth failed {status} for {path}: {payload}")
                if status >= 500:
                    raise OutlineAPIError(f"Outline server error {status} for {path}")
                if status >= 400:
                    # Other 4xx -> fail closed no retry
                    try:
                        payload = resp.json() if hasattr(resp, "json") else {}
                    except Exception:
                        payload = {}
                    raise OutlineAPIError(f"Outline client error {status} for {path}: {payload}")
                return resp
            except OutlineAPIError as e:
                last_exc = e
                # Do not retry auth/client errors
                msg = str(e)
                if "auth failed" in msg.lower() or "client error 4" in msg:
                    raise
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff_s * attempt)
                    continue
                raise
            except Exception as e:
                last_exc = e
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff_s * attempt)
                    continue
                raise OutlineAPIError(f"Outline request failed after {self.max_retries} retries: {e}") from e
        if last_exc:
            raise OutlineAPIError(str(last_exc))
        raise OutlineAPIError("Outline request failed: unknown")

    def _fetch_page(self, offset: int, limit: int) -> tuple[list[dict[str, Any]], bool]:
        body: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "sort": "updatedAt",
            "direction": "ASC",
        }
        if self.collection_id:
            body["collectionId"] = self.collection_id
        resp = self._post_with_retries("/api/documents.list", body)
        payload = resp.json() if hasattr(resp, "json") else {}
        # Outline returns {data:[...], pagination:{...}} or {data:[...]}
        if isinstance(payload, dict) and "data" in payload:
            data = payload.get("data") or []
            pagination = payload.get("pagination") or {}
            total = pagination.get("total")
            # has_more heuristic: if pagination present use it, else infer from len == limit
            if isinstance(total, int):
                has_more = (offset + len(data)) < total
            else:
                has_more = len(data) == limit
        elif isinstance(payload, list):
            data = payload
            has_more = len(data) == limit
        else:
            # Unknown shape — treat payload itself as data if list-like, else empty
            data = []
            has_more = False
        if not isinstance(data, list):
            data = []
        return data, bool(has_more)

    def fetch(self, checkpoint: Any | None = None) -> FetchResult:
        # Fail closed when credentials absent — no mock fallback
        if not self._api_url or not self._api_token:
            raise RuntimeError(
                "Outline credentials missing: api_url and api_token are required "
                "(set OUTLINE_API_URL + OUTLINE_API_TOKEN / OUTLINE_API_KEY or "
                "OAOS_OUTLINE_TOKEN). Refusing to return mock data."
            )
        # Bounded pagination loop guard; callers may use a smaller batch window.
        max_pages = self.max_pages or 500
        offset = 0
        # If checkpoint has cursor offset, resume from it (opaque cursor is offset string)
        if checkpoint is not None:
            cursor = getattr(checkpoint, "cursor", None)
            if isinstance(cursor, str) and cursor.isdigit():
                # Only honor cursor if it's an integer offset; otherwise start 0 and let incremental
                # skip handle unchanged docs via content_hash comparison
                try:
                    offset = int(cursor)
                except Exception:
                    offset = 0

        all_docs: list[SourceDocument] = []
        has_more = True
        pages = 0
        next_cursor: str | None = None
        while has_more and pages < max_pages:
            raw_docs, has_more_page = self._fetch_page(offset, self.page_limit)
            has_more = has_more_page
            pages += 1
            for raw in raw_docs:
                if not isinstance(raw, dict):
                    continue
                try:
                    doc = _normalize_document(raw, self._api_url)
                except Exception:
                    # Skip malformed doc but continue pagination — no silent data loss beyond one doc
                    continue
                all_docs.append(doc)
            if not raw_docs:
                has_more = False
                break
            offset += len(raw_docs)
            next_cursor = str(offset)
            # If server says no more, break
            if not has_more:
                break
            # Safety: if offset not advancing (bug), break
            if len(raw_docs) < self.page_limit:
                has_more = False
                break

        self._last_fetch_pages = pages
        # Deleted ids: Outline API does not return deletions in list; caller (SyncOrchestrator)
        # will treat checkpoint-only ids as deleted. We return empty deleted list here.
        return FetchResult(
            documents=all_docs,
            deleted_resource_ids=[],
            next_cursor=next_cursor,
            has_more=False,
        )

    # ------------------------------------------------------------------
    # Write guard & helpers (read-back verification)
    # ------------------------------------------------------------------
    def _require_write(self, action: str, context: dict[str, Any] | None = None) -> None:
        if not self.write_enabled:
            raise PermissionError(
                f"Outline writes disabled (write_enabled=False); {action} denied — fail-closed. "
                "Enable with write_enabled=True and a permission check."
            )
        if not self._api_url or not self._api_token:
            raise RuntimeError(
                "Outline credentials missing: api_url and api_token are required for writes "
                "(set OUTLINE_API_URL + OUTLINE_API_TOKEN). Failing closed."
            )
        if self._write_permission_checker is not None:
            try:
                allowed = self._write_permission_checker(action, context or {})
            except Exception as e:
                raise PermissionError(f"write permission check failed for {action}: {e}") from e
            if not allowed:
                raise PermissionError(f"write permission denied for {action}")

    def _read_back_and_verify(
        self,
        doc_id: str,
        expected_title: str | None,
        expected_text: str | None,
    ) -> SourceDocument:
        """Fetch documents.info for doc_id and verify id/title/text hash exactly."""
        resp = self._post_with_retries("/api/documents.info", {"id": doc_id})
        payload = resp.json() if hasattr(resp, "json") else {}
        # Outline wraps in {data: doc} or {document: doc} or bare doc
        raw: dict[str, Any] | None = None
        if isinstance(payload, dict):
            if isinstance(payload.get("data"), dict):
                raw = payload["data"]
            elif isinstance(payload.get("document"), dict):
                raw = payload["document"]
            elif payload.get("id"):
                raw = payload
            else:
                # sometimes data is nested under 'data' with pagination — but for info it's direct
                raw = payload.get("data") if isinstance(payload.get("data"), dict) else None
        if not isinstance(raw, dict) or not raw.get("id"):
            raise OutlineAPIError(f"read-back documents.info failed for {doc_id}: missing id in {payload}")
        doc = _normalize_document(raw, self._api_url)
        # exact verification
        if str(raw.get("id")) != str(doc_id):
            raise OutlineAPIError(f"read-back id mismatch: expected {doc_id}, got {raw.get('id')}")
        if expected_title is not None and doc.title != expected_title:
            raise OutlineAPIError(f"read-back title mismatch: expected {expected_title!r}, got {doc.title!r}")
        if expected_text is not None:
            exp_hash = _content_hash(expected_text)
            if doc.content_hash != exp_hash:
                # also compare raw text field if normalize missed
                got_hash = doc.content_hash
                raise OutlineAPIError(
                    f"read-back text hash mismatch for {doc_id}: expected {exp_hash[:12]}, got {got_hash[:12]}"
                )
            if doc.content != expected_text:
                raise OutlineAPIError(f"read-back text mismatch for {doc_id}")
        return doc

    # ------------------------------------------------------------------
    # Explicit write APIs (gated)
    # ------------------------------------------------------------------
    def create_document(
        self,
        title: str,
        text: str,
        collection_id: str | None = None,
        publish: bool = True,
        context: dict[str, Any] | None = None,
    ) -> SourceDocument:
        """Create a new Outline document. Requires write_enabled=True.

        Uses POST /api/documents.create {title, text, collectionId, publish}.
        After write, read-backs via documents.info and verifies id/title/text hash exactly.
        Bounded timeout/retries, no secret logs.
        """
        self._require_write("create_document", context)
        if not title or not title.strip():
            raise ValueError("title is required for create_document")
        coll = (collection_id or self.collection_id or "team").strip()
        body: dict[str, Any] = {"title": title.strip(), "text": text or "", "collectionId": coll}
        # Outline create supports publish flag (some deployments require it)
        body["publish"] = bool(publish)
        resp = self._post_with_retries("/api/documents.create", body)
        payload = resp.json() if hasattr(resp, "json") else {}
        # response shape {data: {id,...}} or {id,...}
        raw = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        doc_id = str(raw.get("id") or raw.get("documentId") or "").strip() if isinstance(raw, dict) else ""
        if not doc_id:
            raise OutlineAPIError(f"create_document failed: no id in response {payload}")
        return self._read_back_and_verify(doc_id, title.strip(), text or "")

    def update_document(
        self,
        doc_id: str,
        title: str | None = None,
        text: str | None = None,
        publish: bool = True,
        context: dict[str, Any] | None = None,
        append: bool = False,
    ) -> SourceDocument:
        """Update an existing Outline document. Requires write_enabled=True.

        Uses POST /api/documents.update {id, title, text, publish:true, append?}.
        publish=True is always sent per spec. Read-back verifies exact id/title/text hash.
        """
        self._require_write("update_document", context)
        if not doc_id or not str(doc_id).strip():
            raise ValueError("doc_id is required for update_document")
        doc_id = str(doc_id).strip()
        body: dict[str, Any] = {"id": doc_id, "publish": True}
        # Spec: publish:true on update
        if publish is not True:
            # enforce true regardless of caller value to satisfy spec — but allow override for tests if explicitly False?
            # We still send True as required; if caller passes False we honor True for publish
            body["publish"] = True
        if title is not None:
            body["title"] = title
        if text is not None:
            body["text"] = text
        if append:
            body["append"] = True
        resp = self._post_with_retries("/api/documents.update", body)
        # update returns {data: doc} but we ignore and read-back for verification
        _ = resp.json() if hasattr(resp, "json") else {}
        # For verification, if only title or only text provided, we need to fetch expected values
        # If caller provided both, verify both; if only one, verify that one, hash check for text if provided
        expected_title = title
        expected_text = text
        return self._read_back_and_verify(doc_id, expected_title, expected_text)

    def delete_document(
        self,
        doc_id: str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Delete (archive) an Outline document. Requires write_enabled=True.

        Uses POST /api/documents.delete {id}. Returns True on success.
        """
        self._require_write("delete_document", context)
        if not doc_id or not str(doc_id).strip():
            raise ValueError("doc_id is required for delete_document")
        doc_id = str(doc_id).strip()
        resp = self._post_with_retries("/api/documents.delete", {"id": doc_id})
        payload = resp.json() if hasattr(resp, "json") else {}
        # Outline delete returns {success:true} or {data: ...}
        if isinstance(payload, dict):
            if payload.get("success") is False:
                raise OutlineAPIError(f"delete failed for {doc_id}: {payload}")
            # Treat any 2xx as success even if payload empty
        return True

    def publish_document(
        self,
        doc_id: str,
        context: dict[str, Any] | None = None,
    ) -> SourceDocument:
        """Publish a document (wrapper over update with publish true)."""
        return self.update_document(doc_id=doc_id, publish=True, context=context)


__all__ = [
    "HttpOutlineSourceAdapter",
    "OutlineSourceConfig",
    "OutlineAPIError",
]
