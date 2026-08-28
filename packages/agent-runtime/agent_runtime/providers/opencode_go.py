"""OpenCode provider — HTTP + local binary + mock fallback.

Chain: httpx (OpenAI-compat /api/chat) -> local binary (opencode run) -> mock

- Binary detection: OPENCODE_BIN / OPENCODE_BINARY env, `path` field if executable,
  `which opencode` (shutil.which), common paths.
- `path` field: if file/executable and name contains "opencode" -> binary path,
  otherwise treated as project/cwd path for binary execution.
- Health check: HTTP /health or /v1/models, fallback to `opencode --version`.
- Streaming: call(..., stream=True) or stream() async generator.

Optional dependency: httpx for HTTP, otherwise binary/mock.
Binary is optional — customer servers may not have it installed.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------

def _mock(model: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = str(m.get("content", ""))[:200]
            break
    return {
        "id": f"mock-opencode-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"[mock:opencode:{model}] echo: {last}" if last else f"[mock:opencode:{model}] hello", "tool_calls": []}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _mock_stream_chunks(model: str, content: str) -> list[dict[str, Any]]:
    words = content.split()
    if not words:
        words = ["hello"]
    chunks: list[dict[str, Any]] = []
    for w in words:
        chunks.append({
            "id": f"mock-opencode-stream-{uuid.uuid4().hex[:6]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"content": w + " "}, "finish_reason": None}],
        })
    chunks.append({
        "id": f"mock-opencode-stream-{uuid.uuid4().hex[:6]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    return chunks


# ---------------------------------------------------------------------------
# Binary detection helpers
# ---------------------------------------------------------------------------

_COMMON_BINARY_PATHS = [
    "/usr/local/bin/opencode",
    "/usr/bin/opencode",
    "/opt/opencode/bin/opencode",
    str(Path.home() / ".opencode" / "bin" / "opencode"),
    str(Path.home() / "bin" / "opencode"),
]


def _is_executable(p: str | Path) -> bool:
    try:
        pp = Path(p)
        return pp.is_file() and os.access(str(pp), os.X_OK)
    except Exception:
        return False


def _looks_like_binary_path(p: str) -> bool:
    """Heuristic: filename contains opencode and is not a project dir."""
    if not p:
        return False
    low = p.lower()
    # must contain opencode and not look like project dir (no trailing / without binary name)
    if "opencode" not in low:
        return False
    # if path ends with opencode or opencode.exe -> binary
    name = Path(p).name.lower()
    if name in ("opencode", "opencode.exe", "opencode.bin"):
        return True
    # if parent contains bin and name is opencode
    if name == "opencode":
        return True
    # contains opencode string and is file-like (has no spaces? has extension?)
    # be lenient: if string contains opencode and path exists as file, treat as binary
    if Path(p).exists() and Path(p).is_file():
        return True
    # env-style bare binary name
    if p.strip().lower() == "opencode":
        return True
    return False


def resolve_binary_path(path_hint: str | None = None) -> str | None:
    """Resolve opencode binary location.

    Priority:
    1. OPENCODE_BIN / OPENCODE_BINARY env
    2. path_hint if it looks like binary path and executable
    3. which opencode
    4. common paths
    Returns absolute path or None.
    """
    # 1. env overrides
    for env_key in ("OPENCODE_BIN", "OPENCODE_BINARY", "OAOS_OPENCODE_BIN", "OAOS_OPENCODE_BINARY"):
        v = os.getenv(env_key)
        if v and v.strip():
            cand = v.strip()
            # if bare name, resolve via which
            if "/" not in cand and "\\" not in cand:
                w = shutil.which(cand)
                if w:
                    return w
                # bare but not found -> still return cand if executable check passes
                if _is_executable(cand):
                    return cand
                # fallback return which result or cand
                return w or cand
            if _is_executable(cand):
                return cand
            # env points to non-executable but existing? return anyway if file exists
            if Path(cand).exists():
                return cand
            # still return cand (caller will check executability)
            return cand

    # 2. path field hint
    if path_hint and _looks_like_binary_path(path_hint):
        # if path_hint exists and executable, use it
        if _is_executable(path_hint):
            return str(Path(path_hint).resolve())
        # if hint is bare "opencode"
        if path_hint.strip().lower() == "opencode":
            w = shutil.which("opencode")
            if w:
                return w
        # if hint is absolute but not yet executable (maybe not installed yet), still consider
        p = Path(path_hint)
        if p.is_absolute() and p.exists():
            return str(p)
        # relative with opencode name -> try which
        if "opencode" in path_hint.lower():
            w = shutil.which(path_hint)
            if w:
                return w
            # try generic which
            w2 = shutil.which("opencode")
            if w2:
                return w2

    # 3. which
    w = shutil.which("opencode")
    if w:
        return w

    # 4. common paths
    for cand in _COMMON_BINARY_PATHS:
        if _is_executable(cand):
            return cand

    return None


def resolve_project_path(path_hint: str | None = None) -> str | None:
    """If path_hint is NOT a binary path, treat as project directory."""
    if not path_hint:
        return None
    if _looks_like_binary_path(path_hint):
        # could be binary -> not project
        # but if hint is directory, it's not binary
        p = Path(path_hint)
        if p.is_dir():
            return str(p.resolve())
        # if file executable -> binary, not project
        if _is_executable(path_hint):
            return None
        # ambiguous: contains opencode string but not executable -> could be project named opencode?
        # prefer binary detection already done; here treat as None
        # check if hint exists as dir
        if p.exists() and p.is_dir():
            return str(p.resolve())
        return None
    # not binary-like -> treat as project path if dir exists, otherwise return as-is
    p = Path(path_hint)
    if p.exists():
        return str(p.resolve())
    # return hint as-is for cwd usage (may be relative)
    return path_hint


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class OpenCodeProvider:
    """OpenCode via HTTP API + local binary fallback.

    Env: OPENCODE_API_URL / OPENCODE_BASE_URL default http://localhost:4096
         OPENCODE_BIN / OPENCODE_BINARY — explicit binary path
         OPENCODE_PATH — project path (alternative to path field)
    Kwargs:
        api_key, base_url, model, path, binary_path
    path: binary path OR project path (auto-detected).
    binary_path: explicit binary override.
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None, **kwargs: Any) -> None:  # type: ignore
        self.api_key = api_key or os.getenv("OPENCODE_API_KEY") or os.getenv("OAOS_OPENCODE_API_KEY") or ""
        self.base_url = (base_url or kwargs.get("baseUrl") or kwargs.get("url") or os.getenv("OPENCODE_API_URL") or os.getenv("OPENCODE_BASE_URL") or os.getenv("OAOS_OPENCODE_BASE_URL") or "http://localhost:4096").rstrip("/")
        self.default_model = model or kwargs.get("model") or os.getenv("OPENCODE_MODEL") or os.getenv("OAOS_OPENCODE_MODEL") or "opencode-default"
        # path can be binary or project; explicit binary_path kw overrides
        raw_path = kwargs.get("path") or kwargs.get("project_path") or kwargs.get("cwd") or os.getenv("OPENCODE_PATH") or os.getenv("OPENCODE_PROJECT_PATH") or ""
        self.path: str = str(raw_path) if raw_path is not None else ""
        explicit_bin = kwargs.get("binary_path") or kwargs.get("binary") or kwargs.get("opencode_bin") or None
        if explicit_bin:
            self._explicit_binary: str | None = str(explicit_bin)
        else:
            self._explicit_binary = None
        self._cached_binary: str | None = None  # lazy sentinel replaced below
        self._binary_resolved: bool = False

    def _get_binary(self) -> str | None:
        if self._binary_resolved:
            return self._cached_binary
        # resolve
        hint = self._explicit_binary or self.path or None
        found = resolve_binary_path(hint)
        self._cached_binary = found
        self._binary_resolved = True
        return found

    def _get_project_dir(self) -> str | None:
        # if path is project dir, return it; otherwise None -> cwd
        if self._explicit_binary:
            # explicit binary means path field is project
            return resolve_project_path(self.path)
        # auto: if path looks like binary, no project
        proj = resolve_project_path(self.path)
        return proj

    # -- health check --

    async def health_check(self) -> dict[str, Any]:
        """Check opencode availability.

        Tries HTTP /health, /v1/models, then binary --version.
        Returns {status: ok|degraded|unavailable, http: bool, binary: bool, ...}
        """
        http_ok = False
        http_detail: str = ""
        # try httpx http health
        try:
            import httpx  # type: ignore
            async with httpx.AsyncClient(timeout=2.0) as client:
                for ep in ("/health", "/v1/models", "/api/health", "/v1/chat/completions"):
                    try:
                        url = f"{self.base_url}{ep}"
                        if ep == "/v1/chat/completions":
                            # lightweight GET may 405; use GET /v1/models instead prefer
                            continue
                        resp = await client.get(url)
                        if resp.status_code < 500:
                            http_ok = True
                            http_detail = f"{ep}:{resp.status_code}"
                            break
                    except Exception:
                        continue
                if not http_ok:
                    # try POST with empty payload to see if server responds (not 0)
                    try:
                        resp = await client.get(f"{self.base_url}/v1/models")
                        if resp.status_code < 500:
                            http_ok = True
                            http_detail = "v1/models"
                    except Exception as e:
                        http_detail = str(e)[:120]
        except ImportError:
            http_detail = "httpx not installed"
        except Exception as e:
            http_detail = str(e)[:120]

        bin_path = self._get_binary()
        bin_ok = False
        bin_version = ""
        if bin_path:
            try:
                # run opencode --version with short timeout in thread
                def _ver() -> tuple[bool, str]:
                    try:
                        r = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=3)
                        out = (r.stdout or r.stderr or "").strip()[:200]
                        ok = r.returncode == 0
                        return ok, out
                    except FileNotFoundError:
                        return False, "not found"
                    except subprocess.TimeoutExpired:
                        return False, "timeout"
                    except Exception as e:
                        return False, str(e)[:120]

                bin_ok, bin_version = await asyncio.to_thread(_ver)
            except Exception as e:
                bin_version = str(e)[:120]

        if http_ok or bin_ok:
            status = "ok" if (http_ok and bin_ok) or http_ok or bin_ok else "degraded"
            # if at least one is ok, status ok
            status = "ok"
        else:
            status = "unavailable"

        return {
            "status": status,
            "http_ok": http_ok,
            "http_detail": http_detail,
            "base_url": self.base_url,
            "binary_ok": bin_ok,
            "binary_path": bin_path,
            "binary_version": bin_version,
            "project_path": self._get_project_dir(),
        }

    def health_check_sync(self) -> dict[str, Any]:
        """Sync variant for admin /test endpoint."""
        try:
            return asyncio.run(self.health_check())
        except RuntimeError:
            # already in event loop — run via new loop
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as ex:
                fut = ex.submit(asyncio.run, self.health_check())
                return fut.result(timeout=5)

    # -- binary call --

    async def _call_via_binary(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None = None,
        timeout_s: float = 30.0,
    ) -> dict[str, Any] | None:
        """Invoke opencode binary.

        Tries:
          1) opencode run --model {model} --format json  (stdin = messages json)
          2) opencode run --model {model}  (last user content as prompt)
        Returns normalized chat.completion dict or None on failure.
        """
        bin_path = self._get_binary()
        if not bin_path or not _is_executable(bin_path) and not Path(bin_path).exists():
            # try which result without executable bit (e.g., mocked path)
            if not bin_path:
                return None
            # if path exists but not executable, still try (tests mock it)
            if not Path(bin_path).exists() and "/" in bin_path:
                return None

        project_dir = self._get_project_dir()

        # Build prompt: last user message content, or concatenated
        prompt_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                prompt_text = str(m.get("content", ""))
                break
        if not prompt_text:
            # concatenate all
            prompt_text = "\n".join(str(m.get("content", "")) for m in messages if m.get("content"))

        # Helper to run subprocess in thread
        def _run(cmd: list[str], inp: str | None, cwd: str | None) -> subprocess.CompletedProcess[str]:
            return subprocess.run(cmd, input=inp, capture_output=True, text=True, timeout=timeout_s, cwd=cwd)

        # Attempt variants
        variants: list[tuple[list[str], str | None]] = [
            # variant A: run --model X --format json with messages json on stdin
            ([bin_path, "run", "--model", model, "--format", "json"], json.dumps({"messages": messages, "model": model})),
            # variant B: run --model X (prompt as last arg)
            ([bin_path, "run", "--model", model, prompt_text], None),
            # variant C: run --model X with prompt via stdin
            ([bin_path, "run", "--model", model], prompt_text),
            # variant D: bare opencode with prompt
            ([bin_path, prompt_text], None),
        ]
        # include tools hint via --tools if provided (best-effort)
        if tools:
            # add tools json to first variant stdin
            pass

        for cmd, inp in variants:
            # filter empty prompt extra arg
            if not prompt_text and len(cmd) > 3 and cmd[-1] == "":
                cmd = cmd[:-1]
            try:
                cwd = project_dir if project_dir and Path(project_dir).is_dir() else None
                result: subprocess.CompletedProcess[str] = await asyncio.to_thread(_run, cmd, inp, cwd)
                stdout = (result.stdout or "").strip()
                stderr = (result.stderr or "").strip()
                if result.returncode != 0 and not stdout:
                    # try next variant
                    continue
                # Parse stdout: try json, else text
                if stdout:
                    # try json chat.completion
                    try:
                        data = json.loads(stdout)
                        if isinstance(data, dict):
                            if "choices" in data:
                                data.setdefault("object", "chat.completion")
                                data.setdefault("model", model)
                                return data
                            if "message" in data:
                                content = data.get("message", {}).get("content", "") or data.get("content", "") or stdout
                                return {
                                    "id": data.get("id", f"opencode-{uuid.uuid4().hex[:8]}"),
                                    "object": "chat.completion",
                                    "created": int(time.time()),
                                    "model": data.get("model", model),
                                    "choices": [{"index": 0, "message": {"role": "assistant", "content": str(content), "tool_calls": []}, "finish_reason": "stop"}],
                                    "usage": data.get("usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
                                }
                            if "content" in data:
                                return {
                                    "id": f"opencode-{uuid.uuid4().hex[:8]}",
                                    "object": "chat.completion",
                                    "created": int(time.time()),
                                    "model": model,
                                    "choices": [{"index": 0, "message": {"role": "assistant", "content": str(data["content"]), "tool_calls": []}, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                }
                            if "text" in data:
                                return {
                                    "id": f"opencode-{uuid.uuid4().hex[:8]}",
                                    "object": "chat.completion",
                                    "created": int(time.time()),
                                    "model": model,
                                    "choices": [{"index": 0, "message": {"role": "assistant", "content": str(data["text"]), "tool_calls": []}, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                }
                    except json.JSONDecodeError:
                        pass
                    # plain text output -> wrap
                    return {
                        "id": f"opencode-{uuid.uuid4().hex[:8]}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": stdout, "tool_calls": []}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    }
                if stderr and result.returncode == 0:
                    return {
                        "id": f"opencode-{uuid.uuid4().hex[:8]}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": stderr, "tool_calls": []}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    }
            except subprocess.TimeoutExpired:
                continue
            except FileNotFoundError:
                return None
            except Exception:
                continue
        return None

    # -- HTTP call helper --

    async def _call_via_http(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None = None,
        timeout_s: float = 15.0,
    ) -> dict[str, Any] | None:
        try:
            import httpx  # type: ignore
        except ImportError:
            return None
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                # Try OpenAI-compat first
                try:
                    resp = await client.post(f"{self.base_url}/v1/chat/completions", json=payload, headers=headers)
                    if resp.status_code < 400:
                        data = resp.json()
                        if "choices" in data:
                            data.setdefault("object", "chat.completion")
                            data.setdefault("model", model)
                            return data
                except Exception:
                    pass
                # fallback /api/chat
                try:
                    resp2 = await client.post(f"{self.base_url}/api/chat", json=payload, headers=headers)
                    if resp2.status_code < 400:
                        data2 = resp2.json()
                        if "choices" in data2:
                            return data2
                        if "message" in data2:
                            content = data2.get("message", {}).get("content", "") or data2.get("content", "")
                            return {
                                "id": data2.get("id", f"opencode-{uuid.uuid4().hex[:8]}"),
                                "object": "chat.completion",
                                "created": int(time.time()),
                                "model": model,
                                "choices": [{"index": 0, "message": {"role": "assistant", "content": str(content), "tool_calls": []}, "finish_reason": "stop"}],
                                "usage": data2.get("usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
                            }
                except Exception:
                    pass
        except Exception:
            return None
        return None

    # -- public API --

    async def call(self, messages: list[dict[str, Any]], model: str | None = None, tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> dict[str, Any]:
        resolved = model or self.default_model
        timeout_s = float(kwargs.get("timeout_s", 15.0))
        stream_requested = bool(kwargs.get("stream", False))

        # streaming requested -> delegate to stream() and collect
        if stream_requested:
            chunks: list[dict[str, Any]] = []
            async for ch in self.stream(messages, model=resolved, tools=tools, **kwargs):
                chunks.append(ch)
            # collect into completion dict
            content_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for ch in chunks:
                delta = ch.get("choices", [{}])[0].get("delta", {})
                if delta.get("content"):
                    content_parts.append(str(delta["content"]))
                if delta.get("tool_calls"):
                    tool_calls.extend(delta["tool_calls"])
            # if chunks contained message style (non-delta), handle
            if not content_parts and not tool_calls:
                # maybe already completion dict
                if chunks and "choices" in chunks[0] and "message" in chunks[0]["choices"][0]:
                    return chunks[0]
            return {
                "id": f"opencode-stream-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": resolved,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(content_parts), "tool_calls": tool_calls}, "finish_reason": "tool_calls" if tool_calls else "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "_stream_chunks": chunks,
            }

        # 1. HTTP
        http_result = await self._call_via_http(messages, resolved, tools=tools, timeout_s=timeout_s)
        if http_result is not None:
            return http_result

        # 2. Binary (only if httpx failed or not available)
        try:
            bin_result = await self._call_via_binary(messages, resolved, tools=tools, timeout_s=timeout_s)
            if bin_result is not None:
                return bin_result
        except Exception:
            pass

        # 3. Mock
        return _mock(resolved, messages, tools=tools)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Streaming generator — tries HTTP SSE, then binary, then mock chunks."""
        resolved = model or self.default_model
        timeout_s = float(kwargs.get("timeout_s", 15.0))

        # Try HTTP streaming via httpx
        try:
            import httpx  # type: ignore
            headers: dict[str, str] = {"Content-Type": "application/json", "Accept": "text/event-stream"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            payload: dict[str, Any] = {"model": resolved, "messages": messages, "stream": True}
            if tools:
                payload["tools"] = tools
            try:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    # attempt stream with timeout
                    try:
                        async with client.stream("POST", f"{self.base_url}/v1/chat/completions", json=payload, headers=headers) as resp:
                            if resp.status_code < 400:
                                async for line in resp.aiter_lines():
                                    if not line:
                                        continue
                                    if line.startswith("data:"):
                                        data_str = line[5:].strip()
                                        if data_str == "[DONE]":
                                            break
                                        try:
                                            chunk = json.loads(data_str)
                                            yield chunk
                                        except Exception:
                                            yield {"id": f"opencode-{uuid.uuid4().hex[:8]}", "object": "chat.completion.chunk", "model": resolved, "choices": [{"index": 0, "delta": {"content": data_str}, "finish_reason": None}]}
                                # stream succeeded — Check if we yielded anything
                                # if we got here without error and status ok, consider stream done
                                # Need to signal finish if not already yielded
                                return
                    except Exception:
                        pass
                    # try non-streaming http then chunk it
                    http_result = await self._call_via_http(messages, resolved, tools=tools, timeout_s=timeout_s)
                    if http_result is not None:
                        content = str(http_result.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
                        tcs = http_result.get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
                        if tcs:
                            yield {"id": http_result.get("id", f"opencode-{uuid.uuid4().hex[:8]}"), "object": "chat.completion.chunk", "model": resolved, "choices": [{"index": 0, "delta": {"tool_calls": tcs}, "finish_reason": None}]}
                            yield {"id": http_result.get("id", ""), "object": "chat.completion.chunk", "model": resolved, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
                            return
                        for ch in _mock_stream_chunks(resolved, content or "mock stream"):
                            yield ch
                        return
            except Exception:
                pass
        except ImportError:
            pass

        # Try binary streaming (run binary and stream stdout lines)
        bin_path = self._get_binary()
        if bin_path:
            try:
                bin_result = await self._call_via_binary(messages, resolved, tools=tools, timeout_s=timeout_s)
                if bin_result is not None:
                    content = str(bin_result.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
                    for ch in _mock_stream_chunks(resolved, content):
                        yield ch
                    return
            except Exception:
                pass

        # Fallback mock streaming
        mock = _mock(resolved, messages, tools=tools)
        content = str(mock["choices"][0]["message"]["content"])
        for ch in _mock_stream_chunks(resolved, content):
            yield ch
            await asyncio.sleep(0)

