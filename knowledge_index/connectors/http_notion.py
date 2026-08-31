"""Real read-only Notion HTTP SourceAdapter for RAG incremental sync.

Mirrors HttpOutlineSourceAdapter pattern (§0.4.1):
- Read-only, stdlib HTTP, no mock fallback
- Configurable API URL/token via ctor or env (NOTION_API_KEY, NOTION_TOKEN, OAOS_NOTION_TOKEN, NOTION_API_URL)
- Pagination (start_cursor/page_size), bounded timeout/retries, fail-closed
- Normalized SourceDocument: resource_id, source_uri, title, content, source_updated_at, acl_version, acl {groups, users}, content_hash

Notion API: POST {apiUrl}/v1/search {query, filter, page_size, start_cursor}
Response: {results: [page/database], next_cursor, has_more}
Each result has id, title (via properties), last_edited_time, url, parent, properties.

Security: if credentials absent or HTTP error exhausts retries -> raise RuntimeError (SyncOrchestrator treats as failed, checkpoint not advanced).
No silent mock fallback.

Transport injection: `http_client` may be object with post(url, headers, json, timeout) -> resp
or get(url, headers, timeout) -> resp where resp has .status_code and .json().
If None, stdlib urllib is used.
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


def _resolve_api_url(explicit: str | None) -> str:
    value = explicit.strip() if explicit is not None else ""
    if not value:
        for k in ("NOTION_API_URL", "OAOS_NOTION_URL", "OAOS_NOTION_API_URL"):
            value = os.environ.get(k, "").strip()
            if value:
                break
    if not value:
        value = "https://api.notion.com"
    value = value.rstrip("/")
    if value.endswith("/v1"):
        value = value[:-3].rstrip("/")
    return value


def _resolve_api_token(explicit: str | None) -> str:
    if explicit is not None:
        return explicit.strip()
    for k in ("NOTION_API_KEY", "NOTION_TOKEN", "OAOS_NOTION_TOKEN", "OAOS_NOTION_API_KEY", "NOTION_API_TOKEN"):
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return ""


class NotionAPIError(RuntimeError):
    """Notion HTTP API error."""


@dataclass
class NotionSourceConfig:
    api_url: str
    api_token: str
    timeout_s: float = 10.0
    max_retries: int = 3
    page_limit: int = 25
    retry_backoff_s: float = 0.2
    query: str | None = None

    def validate(self) -> None:
        if not self.api_url or not self.api_token:
            raise RuntimeError(
                "Notion credentials missing: api_url and api_token are required "
                "(set NOTION_API_KEY / NOTION_TOKEN or OAOS_NOTION_TOKEN). Failing closed — no mock fallback."
            )
        if not (1 <= self.timeout_s <= 60):
            raise ValueError(f"timeout_s must be 1..60, got {self.timeout_s}")
        if not (1 <= self.max_retries <= 8):
            raise ValueError(f"max_retries must be 1..8, got {self.max_retries}")
        if not (1 <= self.page_limit <= 100):
            raise ValueError(f"page_limit must be 1..100, got {self.page_limit}")


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
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return s


def _extract_title(raw: dict[str, Any]) -> str:
    # Notion page title is in properties.title.title[0].plain_text or properties.Name etc.
    props = raw.get("properties") or {}
    # Try common title keys
    for key in ("title", "Title", "Name", "name", "Page"):
        val = props.get(key)
        if isinstance(val, dict):
            # title type: {"title": [{"plain_text": "..."}]}
            title_arr = val.get("title") or []
            if isinstance(title_arr, list) and title_arr:
                parts = []
                for item in title_arr:
                    if isinstance(item, dict):
                        parts.append(str(item.get("plain_text") or item.get("text", {}).get("content", "") or ""))
                joined = "".join(parts).strip()
                if joined:
                    return joined
            # rich_text fallback
            rt = val.get("rich_text") or []
            if isinstance(rt, list) and rt:
                parts = []
                for item in rt:
                    if isinstance(item, dict):
                        parts.append(str(item.get("plain_text") or ""))
                joined = "".join(parts).strip()
                if joined:
                    return joined
    # Fallback: use id-based title
    if raw.get("properties") and raw.get("id"):
        return str(raw.get("id"))[:8]
    # Try direct title field
    if raw.get("title"):
        t = raw.get("title")
        if isinstance(t, str):
            return t
        if isinstance(t, list) and t and isinstance(t[0], dict):
            return str(t[0].get("plain_text") or "")
    return "Untitled"


def _extract_content(raw: dict[str, Any]) -> str:
    # Content: try to extract from properties rich_text + fallback
    content_parts: list[str] = []
    props = raw.get("properties") or {}
    for k, v in props.items():
        if not isinstance(v, dict):
            continue
        # rich_text
        if "rich_text" in v and isinstance(v["rich_text"], list):
            for item in v["rich_text"]:
                if isinstance(item, dict):
                    t = item.get("plain_text") or ""
                    if t:
                        content_parts.append(str(t))
        # title
        if "title" in v and isinstance(v["title"], list):
            for item in v["title"]:
                if isinstance(item, dict):
                    t = item.get("plain_text") or ""
                    if t:
                        content_parts.append(str(t))
    # Also check direct text/content fields
    for k in ("text", "content", "body", "description"):
        val = raw.get(k)
        if isinstance(val, str) and val.strip():
            content_parts.append(val.strip())
    if not content_parts:
        # Use title as minimal content
        title = _extract_title(raw)
        if title and title != "Untitled":
            content_parts.append(title)
    return "\n".join(content_parts)


def _derive_acl_version(raw: dict[str, Any], updated_at_iso: str) -> str:
    for k in ("acl_version", "aclVersion", "version"):
        v = raw.get(k)
        if v:
            return str(v)
    # Derive from last_edited_time + properties hash
    props_hash = hashlib.sha256(json.dumps(raw.get("properties", {}), sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
    uh = hashlib.sha256(updated_at_iso.encode()).hexdigest()[:6]
    return f"v-{props_hash}-{uh}"


def _parse_acl(raw: dict[str, Any]) -> dict[str, Any]:
    acl = raw.get("acl")
    if isinstance(acl, dict) and acl:
        groups = acl.get("groups") or acl.get("allowedGroups") or []
        users = acl.get("users") or acl.get("allowedUsers") or []
        out: dict[str, Any] = {}
        if groups:
            out["groups"] = list(groups) if isinstance(groups, list) else [groups]
        if users:
            out["users"] = list(users) if isinstance(users, list) else [users]
        return out if out else dict(acl)
    # Notion permissions are typically workspace-level; default empty (no restriction) as {}
    return {}


def _normalize_document(raw: dict[str, Any], api_url: str) -> SourceDocument:
    page_id = str(raw.get("id") or "").strip().replace("-", "")
    if not page_id:
        raise ValueError("Notion doc missing id")
    # Determine database parent
    parent = raw.get("parent") or {}
    database_id = ""
    if isinstance(parent, dict):
        database_id = str(parent.get("database_id") or "").strip().replace("-", "") or "wiki"
    else:
        database_id = "wiki"
    database_id = database_id or "wiki"
    title = _extract_title(raw)
    content = _extract_content(raw)
    updated_raw = raw.get("last_edited_time") or raw.get("lastEditedTime") or raw.get("created_time") or raw.get("createdTime")
    updated_at_iso = _normalize_updated_at(updated_raw)
    url = str(raw.get("url") or "")
    if not url:
        url = f"https://notion.so/{page_id}"
    acl = _parse_acl(raw)
    acl_version = _derive_acl_version(raw, updated_at_iso)
    resource_id = f"notion/{database_id}/{page_id}"
    ch = _content_hash(content)
    return SourceDocument(
        resource_id=resource_id,
        source_system="notion",
        title=title,
        content=content,
        source_updated_at=updated_at_iso,
        acl_version=acl_version,
        acl=acl,
        source_uri=url,
        content_hash=ch,
    )


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
        return _StdlibResponse(e.code, body)
    except urllib.error.URLError as e:
        raise NotionAPIError(f"network error posting {url}: {e}") from e
    except Exception as e:
        raise NotionAPIError(f"unexpected error posting {url}: {e}") from e


NOTION_VERSION = "2022-06-28"


class HttpNotionSourceAdapter(SourceAdapter):
    """Real Notion HTTP adapter — read-only sync by default, explicit writes gated.

    Reads from Notion API via POST /v1/search with pagination (next_cursor).
    Fails closed if credentials missing or HTTP errors exhaust retries.
    No mock/fixture fallback.
    """

    source_system: str = "notion"

    def __init__(
        self,
        api_url: str | None = None,
        api_token: str | None = None,
        timeout_s: float = 10.0,
        max_retries: int = 3,
        page_limit: int = 25,
        retry_backoff_s: float = 0.2,
        query: str | None = None,
        http_client: Any | None = None,
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
        self.query = query
        self._http_client = http_client
        if allow_writes is not None:
            write_enabled = bool(allow_writes) if not write_enabled else True
        self.write_enabled = bool(write_enabled)
        self._write_permission_checker = write_permission_checker
        self._last_fetch_pages = 0
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
        return {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_VERSION,
        }

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{self._api_url.rstrip('/')}{path}"
        headers = self._headers()
        timeout = self.timeout_s
        if self._http_client is not None:
            return self._http_client.post(url, headers=headers, json=body, timeout=timeout)
        return _stdlib_post(url, headers=headers, json_body=body, timeout=timeout)

    def _post_with_retries(self, path: str, body: dict[str, Any]) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._post(path, body)
                status = getattr(resp, "status_code", 200)
                if status in (401, 403):
                    try:
                        payload = resp.json() if hasattr(resp, "json") else {}
                    except Exception:
                        payload = {}
                    raise NotionAPIError(f"Notion auth failed {status} for {path}: {payload}")
                if status >= 500:
                    raise NotionAPIError(f"Notion server error {status} for {path}")
                if status >= 400:
                    try:
                        payload = resp.json() if hasattr(resp, "json") else {}
                    except Exception:
                        payload = {}
                    raise NotionAPIError(f"Notion client error {status} for {path}: {payload}")
                return resp
            except NotionAPIError as e:
                last_exc = e
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
                raise NotionAPIError(f"Notion request failed after {self.max_retries} retries: {e}") from e
        if last_exc:
            raise NotionAPIError(str(last_exc))
        raise NotionAPIError("Notion request failed: unknown")

    def _fetch_page(self, cursor: str | None) -> tuple[list[dict[str, Any]], str | None, bool]:
        body: dict[str, Any] = {"page_size": self.page_limit, "sort": {"timestamp": "last_edited_time", "direction": "ascending"}}
        if self.query:
            body["query"] = self.query
        # filter to pages only by default; allow databases too
        if cursor:
            body["start_cursor"] = cursor
        resp = self._post_with_retries("/v1/search", body)
        payload = resp.json() if hasattr(resp, "json") else {}
        results = payload.get("results") or payload.get("data") or []
        if not isinstance(results, list):
            results = []
        next_cursor = payload.get("next_cursor")
        has_more = bool(payload.get("has_more"))
        return results, next_cursor, has_more

    def fetch(self, checkpoint: Any | None = None) -> FetchResult:
        if not self._api_url or not self._api_token:
            raise RuntimeError(
                "Notion credentials missing: api_url and api_token are required "
                "(set NOTION_API_KEY / NOTION_TOKEN or OAOS_NOTION_TOKEN). Refusing to return mock data."
            )
        max_pages = 500
        cursor: str | None = None
        if checkpoint is not None:
            c = getattr(checkpoint, "cursor", None)
            if isinstance(c, str) and c:
                cursor = c
        all_docs: list[SourceDocument] = []
        has_more = True
        pages = 0
        next_cursor: str | None = cursor
        while has_more and pages < max_pages:
            raw_docs, next_c, has_more_page = self._fetch_page(cursor)
            has_more = has_more_page
            pages += 1
            for raw in raw_docs:
                if not isinstance(raw, dict):
                    continue
                # Skip unsupported object types (e.g. databases without content extraction)
                if raw.get("object") not in (None, "page", "database"):
                    continue
                try:
                    doc = _normalize_document(raw, self._api_url)
                except Exception:
                    continue
                all_docs.append(doc)
            cursor = next_c
            next_cursor = next_c
            if not raw_docs:
                has_more = False
                break
            if len(raw_docs) < self.page_limit:
                has_more = False
                break
            if not has_more:
                break
        self._last_fetch_pages = pages
        return FetchResult(documents=all_docs, deleted_resource_ids=[], next_cursor=next_cursor, has_more=bool(has_more))
