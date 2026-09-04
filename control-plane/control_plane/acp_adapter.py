"""ACP Adapter — Internal Agent Interface ↔ Hermes ACP (Section 17).

Design: Internal Agent Interface is the canonical contract.
ACP is an adapter — not the canonical protocol. Hermes core is NOT modified.

Wire: Hermes ACP (Client ↔ Agent) via stdio/SSE/WebSocket depending on deployment.
This adapter translates:
  create_session / send_prompt / stream_event  ↔  Hermes ACP messages

Fallback v1.5.1: When ACP endpoint is unavailable (404), fall back to
Hermes Gateway OpenAI-compatible /v1/chat/completions (same LLM as
hermes @openit CoCo) — not Ollama. Uses OAOS_CP_HERMES_API_KEY.
"""
from __future__ import annotations
import asyncio
import json
import re
import uuid
import logging
import os
from typing import AsyncGenerator, Any
import httpx
from .session import SessionRecord
import time as _time

# -- HA: retry (500/429/timeout, 3 retries exponential backoff) + circuit breaker + audit --
def _is_retryable_status(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    try:
        import httpx as _hx  # type: ignore
        if isinstance(exc, _hx.TimeoutException):  # type: ignore
            return True
    except Exception:
        pass
    for attr in ("status_code", "status", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and v in (429, 500, 502, 503, 504):
            return True
    resp = getattr(exc, "response", None)
    if resp is not None:
        sc = getattr(resp, "status_code", None)
        if sc in (429, 500, 502, 503, 504):
            return True
    msg = str(exc).lower()
    if "429" in msg or "too many requests" in msg:
        return True
    if "timeout" in msg or "timed out" in msg:
        return True
    if any(x in msg for x in ("500", "502", "503", "504")):
        return True
    return False

class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, reset_timeout_s: float = 30.0, name: str = "acp"):
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self.name = name
        self._failures = 0
        self._state = "CLOSED"
        self._opened_at: float | None = None

    def can_execute(self) -> bool:
        if self._state == "CLOSED":
            return True
        if self._state == "OPEN":
            if self._opened_at is not None and (_time.monotonic() - self._opened_at) >= self.reset_timeout_s:
                self._state = "HALF_OPEN"
                return True
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._state = "CLOSED"
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = "OPEN"
            self._opened_at = _time.monotonic()

    @property
    def state(self) -> str:
        if self._state == "OPEN" and self._opened_at is not None and (_time.monotonic() - self._opened_at) >= self.reset_timeout_s:
            self._state = "HALF_OPEN"
        return self._state

_acp_circuit_breaker = CircuitBreaker(failure_threshold=3, reset_timeout_s=30.0, name="acp")


def _llm_max_attempts() -> int:
    """Bound one logical Mattermost post to one LLM request by default."""
    raw = os.getenv("OAOS_LLM_MAX_ATTEMPTS", "")
    if raw:
        try:
            return max(1, min(3, int(raw)))
        except ValueError:
            pass
    return 1 if os.getenv("OAOS_ENV", "").strip().lower() in {"production", "prod"} else 3


def _audit_emit(event_type: str, trace_id: str, data: dict):
    try:
        # try audit_model + ledger if available, else no-op
        from audit_model import AuditEvent as _AE, AuditEventType as _AET  # type: ignore
        import uuid as _uuid
        from datetime import datetime, timezone as _tz
        # best-effort: emit to security audit_ledger if importable (control-plane may not have ledger)
        try:
            from audit.audit_ledger.ledger import AuditLedger as _AL  # type: ignore
            pass
        except Exception:
            pass
    except Exception:
        pass
    # fallback: log via default_audit_log if present (llm_runtime style) or just logging
    try:
        import logging
        logging.getLogger(__name__).info(f"audit {event_type} trace={trace_id} data={data}")
    except Exception:
        pass

async def _with_retry_acp(fn, *, max_retries: int = 3, backoff_s: float = 0.2, trace_id: str = ""):
    # check circuit
    if not _acp_circuit_breaker.can_execute():
        _audit_emit("circuit_breaker_open", trace_id, {"breaker": _acp_circuit_breaker.name, "state": _acp_circuit_breaker.state})
        raise RuntimeError(f"circuit breaker OPEN for {_acp_circuit_breaker.name}")
    last: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            res = await fn()
            _acp_circuit_breaker.record_success()
            return res
        except Exception as e:
            if not _is_retryable_status(e):
                _acp_circuit_breaker.record_failure()
                _audit_emit("acp_failure", trace_id, {"error": str(e)[:300], "retryable": False, "attempt": attempt + 1})
                raise
            last = e
            if attempt >= max_retries:
                break
            delay = backoff_s * (2 ** attempt)
            _audit_emit("retry", trace_id, {"attempt": attempt + 1, "max_retries": max_retries, "error": str(e)[:300], "backoff_s": delay})
            await asyncio.sleep(delay)
    assert last is not None
    _acp_circuit_breaker.record_failure()
    _audit_emit("acp_failure", trace_id, {"error": str(last)[:500], "retryable": True, "attempts": max_retries + 1, "breaker_state": _acp_circuit_breaker.state})
    raise last

def _resolve_workspace_for_session(session: SessionRecord) -> str | None:
    """Lazy workspace path — /home/hermes/workspaces/{tenant}/{agent}/{session}"""
    try:
        from runtime_adapter.workspace import WorkspaceResolver  # type: ignore
        return str(WorkspaceResolver().resolve(session.tenant_id, session.agent_id, session.session_id))
    except Exception:
        try:
            import re as _re
            safe = lambda v: _re.sub(r"[^a-zA-Z0-9._-]", "_", str(v))[:64] or "default"
            return f"/home/hermes/workspaces/{safe(session.tenant_id)}/{safe(session.agent_id)}/{safe(session.session_id)}"
        except Exception:
            return None

class ACPAdapter:
    """Hermes ACP adapter — single integration point (Section 17)."""

    def __init__(self, hermes_base_url: str, timeout_s: float = 30.0):
        self.hermes_base_url = hermes_base_url.rstrip("/")
        self.timeout_s = timeout_s

    # ── Adaptive Profile — Response Policy seam (LLM call boundary) ──
    def _resolve_policy_sync(self, session: SessionRecord, current_instruction: dict | None = None) -> dict:
        """Sync policy resolve with safe default fallback, no leakage."""
        try:
            from control_plane.adaptive_profile.hook import get_response_policy, DEFAULT_POLICY
            task_type = getattr(session, "security_domain", None) or "general_chat"
            # map general -> general_chat for profile
            if task_type == "general":
                task_type = "general_chat"
            policy = get_response_policy(session.tenant_id, session.user_id, task_type, current_instruction or {})
            # ensure minimal keys only
            allowed = set(DEFAULT_POLICY.keys())
            return {k: v for k, v in policy.items() if k in allowed}
        except Exception:
            try:
                from control_plane.adaptive_profile.engine import DEFAULT_POLICY as _DP
                merged = dict(_DP)
                cur = current_instruction or {}
                for k in merged:
                    if k in cur:
                        merged[k] = cur[k]
                return merged
            except Exception:
                return {"conclusion_first": False, "verbosity": "medium", "technical_depth": "medium", "evidence_requirement": "medium", "challenge_assumptions": False, "alternatives": 1, "confirmation_level": "medium"}

    async def _resolve_policy_async(self, session: SessionRecord, current_instruction: dict | None = None) -> dict:
        try:
            from control_plane.adaptive_profile.hook import get_response_policy_async, DEFAULT_POLICY
            task_type = getattr(session, "security_domain", None) or "general_chat"
            if task_type == "general":
                task_type = "general_chat"
            policy = await get_response_policy_async(session.tenant_id, session.user_id, task_type, current_instruction or {})
            allowed = set(DEFAULT_POLICY.keys())
            return {k: v for k, v in policy.items() if k in allowed}
        except Exception:
            return self._resolve_policy_sync(session, current_instruction)

    def resolve_policy(self, session: SessionRecord, current_instruction: dict | None = None) -> dict:
        """Public adapter seam — Control Plane/ACP LLM boundary."""
        return self._resolve_policy_sync(session, current_instruction)

    def build_llm_messages(self, session: SessionRecord, prompt_text: str, policy: dict | None = None, system_base: str | None = None, file_ids: list[str] | None = None, attachment_refs: list[dict] | None = None) -> list[dict]:
        """Build LLM messages with minimal Response Policy injected (no scores). Supports multimodal file_ids/attachments (no model selection)."""
        try:
            from control_plane.adaptive_profile.hook import default_hook
            from control_plane.adaptive_profile.engine import DEFAULT_POLICY
            if policy is None:
                policy = self._resolve_policy_sync(session)
            # ensure minimal
            allowed = set(DEFAULT_POLICY.keys())
            policy = {k: v for k, v in policy.items() if k in allowed}
            injection = default_hook.format_prompt_injection(policy)
        except Exception:
            injection = ""
            policy = {}
        base = system_base or (
            f"You are Open Agent OS personal agent {session.agent_id} for user {session.user_id} "
            f"(tenant {session.tenant_id}, session {session.session_id})."
        )
        system_content = base
        if injection:
            system_content = base + "\n\n" + injection
        # Ensure no raw scores leak
        if "global_score" in system_content or "sample_count" in system_content:
            system_content = base
        # Multimodal current-session message contract — no model selection, direct delivery via active runtime
        # Hermes runtime reliably consumes OpenAI multimodal image_url only when URL is data URL or accessible path.
        # Prefer bounded base64/data URL from bridge's attachment_refs; fallback to file:// only when no bytes available.
        # Non-image refs (kind=text_preview/stored_only or MIME not image/*) NEVER become image_url parts:
        # their masked preview / stored-only note is already composed into prompt_text by the bridge.
        # Vault paths are citations only — never emitted as absolute file:// URLs.
        # A CP-side bounded extractor may fill ref["extracted_text"]; when present it is
        # appended as a bounded, masked citation text part (bridge never extracts).
        user_content: str | list[dict] = prompt_text
        if file_ids or attachment_refs:
            parts: list[dict] = [{"type": "text", "text": prompt_text}]
            refs = attachment_refs or []

            def _mask_extracted(text: str) -> str:
                try:
                    masked = re.sub(
                        r'("(?:client_secret|private[\s_\-]*key|refresh[\s_\-]*token|access[\s_\-]*token|api[\s_\-]*key|passw(?:or)?d)"\s*:\s*")[^"]*(")',
                        lambda m: f"{m.group(1)}***{m.group(2)}",
                        text or "", flags=re.IGNORECASE,
                    )
                    return re.sub(
                        r"(client_secret|private[\s_\-]*key|refresh[\s_\-]*token|access[\s_\-]*token|api[\s_\-]*key|passw(?:or)?d)\s*([:=]|=>)\s*\S+",
                        lambda m: f"{m.group(1)}{m.group(2)}***",
                        masked, flags=re.IGNORECASE,
                    )
                except Exception:
                    return text or ""

            def _citation_name(ref: dict | None) -> str:
                try:
                    raw = str((ref or {}).get("filename") or "attachment")
                except Exception:
                    raw = "attachment"
                return _mask_extracted(raw)[:120] or "attachment"

            def _citation_path(ref: dict | None) -> str:
                try:
                    vp = str((ref or {}).get("vault_path") or "")
                except Exception:
                    vp = ""
                # citation only: relative owner-scoped path; never absolute or file://
                if not vp or vp.startswith("/") or vp.startswith("file://") or "://" in vp:
                    return _citation_name(ref)
                return vp[:300]

            def _is_image_ref(ref: dict | None) -> bool:
                """True only for image attachments: kind=image or MIME image/*.

                Legacy refs without kind fall back to MIME: known non-image MIME
                returns False; unknown/empty MIME stays True for back-compat.
                """
                if not isinstance(ref, dict):
                    return False
                kind = str(ref.get("kind") or "").strip().lower()
                if kind == "image":
                    return True
                if kind in ("text_preview", "stored_only", "stored", "text", "document", "file"):
                    return False
                raw_mime = (
                    ref.get("mime_type") or ref.get("mimeType") or ref.get("mime") or ""
                )
                raw_mime = str(raw_mime).strip().lower().split(";")[0].strip()
                if kind:
                    # unknown kind string — trust MIME when known
                    return raw_mime.startswith("image/") if raw_mime else True
                if raw_mime:
                    return raw_mime.startswith("image/")
                return True

            fids = file_ids or [r.get("attachment_id") or r.get("file_id") or r.get("vault_path") for r in refs if isinstance(r, dict)]
            # index refs by common ids for data_url lookup
            def _ref_for_fid(fid: str) -> dict | None:
                for r in refs:
                    if not isinstance(r, dict):
                        continue
                    if fid in (r.get("file_id"), r.get("attachment_id"), r.get("vault_path"), r.get("filename")):
                        return r
                    # also match if fid is vault_path and ref has it
                    if r.get("vault_path") == fid or r.get("file_id") == fid:
                        return r
                return None
            for fid in (fids or []):
                ref = _ref_for_fid(str(fid))
                if ref is not None and not _is_image_ref(ref):
                    # Non-image attachment: skip image_url entirely. The bridge
                    # already composed its masked text preview / stored-only
                    # note into prompt_text; sending it as image_url/file://
                    # breaks the LLM call (TXT E2E timeout cause).
                    # Optional CP-side bounded extraction: a pre-filled,
                    # masked extracted_text citation may still be appended.
                    try:
                        _ext = (ref or {}).get("extracted_text")
                    except Exception:
                        _ext = None
                    if isinstance(_ext, str) and _ext.strip():
                        _bounded = _mask_extracted(_ext.strip()[:20000])
                        parts.append({
                            "type": "text",
                            "text": f"[첨부 텍스트 추출: {_citation_name(ref)} ({_citation_path(ref)})]\n{_bounded}",
                        })
                    continue
                data_url = None
                mime = "image/png"
                if ref:
                    raw = (ref.get("mime_type") or ref.get("mimeType") or "image/png")
                    mime = str(raw).strip().lower().split(";")[0].strip() or "image/png"
                    if not mime.startswith("image/"):
                        # Known non-image MIME on a ref that passed the image
                        # gate (e.g. legacy kind-less ref): do not coerce to
                        # image/png, do not emit image_url at all.
                        continue
                    # bounded data URL from bridge's authenticated download
                    cand = ref.get("data_url") or ref.get("dataUrl") or ref.get("base64") or ""
                    if isinstance(cand, str) and cand:
                        cand = cand.strip()
                        if cand.startswith("data:"):
                            # validate is image data URL
                            if cand.startswith("data:image/"):
                                data_url = cand
                            else:
                                # reject non-image data URL, fallback
                                data_url = None
                        elif len(cand) > 20 and cand[:16].replace("/","+").replace("_","/"):  # heuristic base64
                            # raw base64 -> build data URL with MIME, bounded
                            # guard length: decoded must be <=20MB (already enforced by bridge)
                            data_url = f"data:{mime};base64,{cand}"
                        else:
                            data_url = None
                if data_url:
                    parts.append({"type": "image_url", "image_url": {"url": data_url}, "file_id": str(fid)})
                else:
                    # Fallback: only if no bounded bytes available; preserves tenant/user/session/channel/post/root context via existing headers
                    # No model/provider selection here; Hermes will attempt file:// only if path is actually accessible.
                    # Vault paths are citations only: never emit an absolute path or vault_path as file://.
                    _sfid = str(fid)
                    if _sfid.startswith("/") or _sfid.startswith("file://") or "://" in _sfid:
                        continue
                    parts.append({"type": "image_url", "image_url": {"url": f"file://{_sfid}"}, "file_id": _sfid})
            user_content = parts
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

    # Back-compat naming
    _build_messages_with_policy = build_llm_messages

    def _headers(self, session: SessionRecord) -> dict[str, str]:
        # AgentContext propagated as headers (Section 18) + workspace
        headers = {
            "X-Tenant-Id": session.tenant_id,
            "X-User-Id": session.user_id,
            "X-Agent-Id": session.agent_id,
            "X-Session-Id": session.session_id,
            "X-Trace-Id": session.trace_id,
            "X-Security-Domain": session.security_domain,
            "X-OAOS-Session-Namespace": getattr(session, "session_namespace", "oaos:mattermost"),
            "X-OAOS-Runtime-Provider": getattr(session, "runtime_provider", "opencode-go"),
            "X-OAOS-Runtime-Model": getattr(session, "runtime_model", "muse-spark-1.2-contributor"),
        }
        # Lazy workspace header — per-session isolation (§16A.3.1)
        ws = getattr(session, "workspace", None)
        if not ws:
            ws = _resolve_workspace_for_session(session)
        if ws:
            headers["X-Workspace"] = ws
            headers["X-Workspace-Path"] = ws
        return headers

    def _hermes_api_key(self) -> str:
        # Resolve the live OAOS-owned environment on every request. Do not retain
        # an import-time settings value after the explicit key is removed.
        # Never use Hermes-global files or another user's credential.
        return os.getenv("OAOS_CP_HERMES_API_KEY", "") or os.getenv("API_SERVER_KEY", "") or ""

    def _hermes_model(self) -> str:
        try:
            from .config import settings
            m = getattr(settings, "hermes_model", "") or ""
            if m:
                return m
        except Exception:
            pass
        return os.getenv("OAOS_CP_HERMES_MODEL", "") or "hermes-agent"

    def _registration_preferences(self, session: SessionRecord) -> dict[str, str]:
        """Load owner-scoped onboarding preferences from the registration store.

        The registration gate persists the user's requested honorific and response
        style in Redis. Those values are conversation policy, not an agent display
        name, so they must be injected explicitly at the Gateway boundary.
        """
        key = f"oaos:registration:{session.tenant_id}:{session.user_id}"
        try:
            import redis as _redis
            url = os.getenv("REDIS_URL") or os.getenv("OAOS_REDIS_URL")
            if not url:
                return {}
            raw = _redis.Redis.from_url(url, decode_responses=True, socket_timeout=1.0).get(key)
            record = json.loads(raw) if raw else {}
            answers = record.get("answers") if isinstance(record, dict) else {}
            if not isinstance(answers, dict):
                return {}
            result = {}
            for field in ("honorific", "response_style"):
                value = answers.get(field)
                if isinstance(value, str) and value.strip():
                    result[field] = value.strip()[:200]
            return result
        except Exception:
            # Preference loading must never block the normal response path.
            return {}

    async def create_session_remote(self, session: SessionRecord, workspace: str | None = None) -> dict[str, Any]:
        """POST /acp/sessions — create Hermes-side session. Falls back to local if Hermes unavailable (dev)."""
        url = f"{self.hermes_base_url}/acp/sessions"
        ws = workspace or getattr(session, "workspace", None) or _resolve_workspace_for_session(session)
        payload = {
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "user_id": session.user_id,
            "tenant_id": session.tenant_id,
            "security_domain": session.security_domain,
            "trace_id": session.trace_id,
            "workspace": ws,
            "workspace_path": ws,
        }
        # remove None workspace if resolver failed (keep key but not None)
        if ws is None:
            payload.pop("workspace", None)
            payload.pop("workspace_path", None)
        async def _do():
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(url, json=payload, headers=self._headers(session))
                r.raise_for_status()
                return r.json()
        try:
            return await _with_retry_acp(_do, max_retries=_llm_max_attempts() - 1, backoff_s=0.2, trace_id=session.trace_id)
        except Exception as e:
            # Dev fallback — Hermes not yet running
            return {"status": "local_fallback", "reason": str(e), "session_id": session.session_id, "workspace": ws}

    def _acp_enabled(self) -> bool:
        """Whether this deployment exposes Hermes ACP session endpoints.

        The production Gateway exposes the OpenAI-compatible API on :8642 but
        not /acp/sessions. Keep ACP available as an explicit opt-in so every
        Mattermost turn does not pay for a guaranteed 404 probe.
        """
        return os.getenv("OAOS_CP_HERMES_ACP_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

    async def send_prompt(self, session: SessionRecord, prompt: str, request_id: str, attachment_refs: list[dict] | None = None, file_ids: list[str] | None = None, runtime_context: dict | None = None) -> dict[str, Any]:
        if not self._acp_enabled():
            return {"status": "gateway_fallback", "request_id": request_id, "file_ids": file_ids, "attachment_refs": attachment_refs}
        url = f"{self.hermes_base_url}/acp/sessions/{session.session_id}/prompt"
        payload: dict[str, Any] = {"prompt": prompt, "request_id": request_id, "trace_id": session.trace_id}
        # Multimodal context forwarding — no model selection, just direct delivery via active runtime
        if attachment_refs:
            payload["attachment_refs"] = attachment_refs
            payload["file_ids"] = file_ids or [r.get("attachment_id") or r.get("vault_path") for r in attachment_refs if isinstance(r, dict)]
            # legacy alias
            if len(attachment_refs) == 1:
                payload["attachment_ref"] = attachment_refs[0]
        if file_ids and "file_ids" not in payload:
            payload["file_ids"] = file_ids
        if runtime_context:
            payload["runtime_context"] = runtime_context
            # forward channel/root/post context as well
            for k in ("channel_id", "root_id", "post_id"):
                if runtime_context.get(k):
                    payload.setdefault("context", {})[k] = runtime_context[k]
        async def _do():
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(url, json=payload, headers=self._headers(session))
                r.raise_for_status()
                return r.json()
        try:
            return await _with_retry_acp(_do, max_retries=_llm_max_attempts() - 1, backoff_s=0.2, trace_id=session.trace_id)
        except Exception as e:
            return {"status": "queued_local", "reason": str(e), "request_id": request_id}

    async def stream_events(self, session: SessionRecord) -> AsyncGenerator[dict[str, Any], None]:
        """SSE stream from Hermes — yields StreamEvent dicts (Section 17: stream_event).

        If Hermes ACP stream is unavailable (404), fall back to Hermes Gateway
        /v1/chat/completions (same LLM that powers @openit CoCo) and yield its
        reply as token stream. This keeps Mattermost @agent on the Hermes-configured LLM.
        """
        if self._acp_enabled():
            url = f"{self.hermes_base_url}/acp/sessions/{session.session_id}/stream"
        else:
            url = ""
        try:
            if not url:
                raise RuntimeError("Hermes ACP session endpoints disabled; using Gateway API")
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                async with client.stream("GET", url, headers=self._headers(session)) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(f"stream status {resp.status_code}")
                    async for line in resp.aiter_lines():
                        if not line or line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            data = line[5:].strip()
                            try:
                                yield json.loads(data)
                            except json.JSONDecodeError:
                                yield {"type": "token", "data": {"text": data}}
                    return
        except Exception:
            pass
        # -- Hermes Gateway fallback (standard path — same LLM as @openit) --
        # Retrieve the current prompt plus bounded durable conversation history.
        prompt_text = ""
        _last_file_ids: list[str] | None = None
        _last_arefs: list[dict] | None = None
        try:
            from .session import session_store
            rec = session_store.get_any(session.session_id)
            if rec and rec.prompt_history:
                last = rec.prompt_history[-1]
                prompt_text = last.get("prompt", "") or ""
                _last_file_ids = last.get("file_ids")
                _last_arefs = last.get("attachment_refs")
                # The Gateway is stateless per request. Rehydrate prior turns
                # from the durable session so a new request can remember them.
                prior = rec.prompt_history[:-1][-12:]
                if prior:
                    history = "\n".join(
                        f"사용자 이전 발화: {item.get('prompt', '')}" for item in prior if item.get("prompt")
                    )
                    if history:
                        prompt_text = f"[DURABLE CONVERSATION HISTORY]\n{history}\n[/DURABLE CONVERSATION HISTORY]\n\n현재 사용자 발화: {prompt_text}"
        except Exception:
            pass
        if prompt_text:
            api_key = self._hermes_api_key()
            model = self._hermes_model()
            gateway_url = f"{self.hermes_base_url}/v1/chat/completions"
            # Hermes gateway is at 8642, but config may point to wrong port — fixup if needed
            if ":8001" in gateway_url:
                gateway_url = gateway_url.replace(":8001", ":8642")
            # Owner onboarding preferences are separate from the internal agent id.
            # Never use the Mattermost username as the user's honorific fallback.
            _prefs = self._registration_preferences(session)
            _honorific = _prefs.get("honorific", "").strip()
            _response_style = _prefs.get("response_style", "").strip()
            _dn = getattr(session, "display_name", None)
            _friendly = _dn if _dn and not _dn.startswith("agent:") else "마이"
            # The onboarding answer may contain the literal username (e.g. mykim).
            # The administrator-approved display name is the canonical user-facing
            # identity and must take precedence over that answer for the master.
            _master_user_ids = {"employee:mykim", "mykim"}
            if session.user_id in _master_user_ids:
                _honorific = "마스터/대표님/민영님"
            preference_lines = []
            if _honorific:
                preference_lines.append(f"사용자 호칭 선호: {_honorific}")
            if _response_style:
                preference_lines.append(f"응답 방식 선호: {_response_style}")
            preference_block = "\n".join(preference_lines)
            base_system = (
                f"You are Open Agent OS personal agent {session.agent_id} for user {session.user_id} "
                f"(tenant {session.tenant_id}, session {session.session_id}). "
                f"Your display name is '{_friendly}'. Always refer to yourself as '{_friendly}' if the user asks your name. "
                "You are SEPARATE from Hermes @openit CoCo (company-wide). "
                "Reply in Korean, concise, helpful. Keep identity consistent. "
                "Do not address the user by their Mattermost username or internal id; use the stored user honorific preference."
                + ("\n" + preference_block if preference_block else "")
            )
            # Adaptive Profile: resolve minimal Response Policy (safe fallback, no leakage)
            try:
                _policy = await self._resolve_policy_async(session)
                _msgs = self.build_llm_messages(session, prompt_text, policy=_policy, system_base=base_system, file_ids=_last_file_ids, attachment_refs=_last_arefs)
                system_prompt = _msgs[0]["content"]
                user_msg = _msgs[1]["content"]
            except Exception:
                system_prompt = base_system
                user_msg = prompt_text
            try:
                # Vision requests can legitimately take longer than text-only turns;
                # keep one bounded request under the bridge's 45s confirmation window
                # plus Gateway queue latency, without introducing another provider.
                async with httpx.AsyncClient(timeout=180.0) as client:
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_msg},
                        ],
                        "temperature": 0.7,
                    }
                    r = None
                    max_attempts = _llm_max_attempts()
                    for attempt in range(max_attempts):
                        r = await client.post(
                            gateway_url,
                            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                            json=payload,
                        )
                        _audit_emit("gateway_attempt", session.trace_id, {"request_id": session.session_id, "attempt": attempt + 1, "max_attempts": max_attempts, "status": r.status_code, "path": "gateway"})
                        if r.status_code not in (429, 500, 502, 503, 504) or attempt + 1 >= max_attempts:
                            break
                        retry_after = r.headers.get("Retry-After", "")
                        try:
                            delay = min(5.0, max(0.5, float(retry_after)))
                        except ValueError:
                            delay = 1.0 * (attempt + 1)
                        logging.getLogger(__name__).warning(
                            "Hermes gateway retry status=%s attempt=%d/3 trace=%s delay=%.1fs",
                            r.status_code, attempt + 1, session.trace_id, delay,
                        )
                        await asyncio.sleep(delay)
                    assert r is not None
                    r.raise_for_status()
                    data = r.json()
                    content = ""
                    try:
                        content = data["choices"][0]["message"]["content"] or ""
                    except Exception:
                        content = data.get("content", "") or ""
                    content = content.strip()
                    # Mattermost usernames are internal identifiers, never user-facing
                    # honorifics.  The model can still copy a username from conversation
                    # history despite the system instruction, so enforce the master mapping
                    # at the OAOS boundary before posting the reply.
                    if session.user_id in {"employee:mykim", "mykim"}:
                        content = re.sub(r"(?i)\bmykim님\b", "마스터", content)
                        content = re.sub(r"(?i)\bmykim\b(?=\s*(?:님|씨))", "마스터", content)
                    if content:
                        # yield in bounded chunks to simulate streaming and avoid half-truncation
                        chunk_size = 800
                        for i in range(0, len(content), chunk_size):
                            chunk = content[i:i+chunk_size]
                            if chunk:
                                yield {"type": "token", "data": {"text": chunk}, "trace_id": session.trace_id}
                                await asyncio.sleep(0.02)
                        yield {"type": "done", "data": {}, "trace_id": session.trace_id}
                        return
            except Exception as e:
                # log and fall through — no synthetic, let agent runtime handle
                try:
                    logging.getLogger(__name__).warning(f"Hermes gateway fallback failed: {e}")
                except Exception:
                    pass
        # -- No synthetic fallback — strictly agent runtime only --
        # If Hermes gateway also unreachable, yield done without token so
        # Mattermost posts nothing (agent runtime will recover and retry).
        yield {"type": "done", "data": {}, "trace_id": session.trace_id}
