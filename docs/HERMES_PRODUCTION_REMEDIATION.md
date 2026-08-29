# Hermes Production Remediation — Persisted `/model` Override (`gpt-5.6-luna` / `custom`)

> Authority: Hermes source at `/home/openitsvc/.hermes/hermes-agent` — **not** in this repo.
> OAOS repo is integration-only; this doc describes what to run **on the Hermes host**.

## Bug

`gateway/run.py`:
- `_rehydrate_session_model_override(session_key)` blindly restores `sessions.json` → `model_override` (`model`/`provider`/`base_url`) without validation.
- `_apply_session_model_override()` then shadows `config.yaml` `model.default` / `model.provider` for every subsequent turn.

Production verification shows `model=gpt-5.6-luna` `provider=custom` at `base_url=http://127.0.0.1:10100` (stale local loopback) was persisted and rebroadcast on every gateway restart, overriding the configured primary `muse-spark-1.2-contributor` / `opencode-go`.

Hermes official docs: https://hermes-agent.nousresearch.com/docs (gateway session model — rehydration must re-resolve credentials; credentials never persisted).

## Required Hermes fix (manual on host — do not apply from this repo)

Patch `gateway/run.py` + `gateway/session.py` to validate before shadowing primary:

```python
# gateway/session.py — add helper (or reuse sanitize_model_override)
BLOCKED_MODEL_SUBSTRINGS = ("gpt-5.6-luna", "gpt-5.6-sol")
BLOCKED_URL_SUBSTRINGS = ("127.0.0.1:10100", "localhost:10100")
ALLOWED_PROVIDERS = {"opencode-go","opencode","claude","codex","gemini","openrouter","ollama"} # plus configured custom:<name>

def is_persisted_model_override_valid(override: dict, *, valid_providers, custom_providers) -> bool:
    if override.get("provider","").lower() == "custom": return False
    if any(s in override.get("model","").lower() for s in BLOCKED_MODEL_SUBSTRINGS): return False
    if any(s in str(override.get("base_url","")).lower() for s in BLOCKED_URL_SUBSTRINGS): return False
    if override.get("provider","").lower() not in {p.lower() for p in valid_providers}: return False # fail-closed
    return True
```

```python
# gateway/run.py — guard _rehydrate_session_model_override
def _rehydrate_session_model_override(self, session_key: str) -> None:
    if _peek_session_state(session_key).conversation.model_override is not None:
        return
    persisted = store.get_model_override(session_key)
    if not persisted:
        return
    # --- ADD: validate before shadowing primary ---
    if not is_persisted_model_override_valid(persisted, valid_providers=..., custom_providers=...):
        logger.warning("Dropping invalid persisted model override session=%s %r", session_key, persisted)
        store.set_model_override(session_key, None)  # clear stale
        return
    # ... existing credential re-resolution ...
```

Same guard in `_apply_session_model_override` — if in-memory override is invalid, ignore it and use `(model, runtime_kwargs)` from `config.yaml`.

Clear stale persisted state once (no restart needed to mutate — just clear JSON):

```bash
# on Hermes host, with gateway stopped or via SessionStore API:
python3 -c "
from gateway.session import SessionStore
s=SessionStore()
for k in list(s._entries):
    ov=s.get_model_override(k)
    if ov and (ov.get('provider')=='custom' or 'gpt-5.6-luna' in ov.get('model','').lower() or '127.0.0.1:10100' in ov.get('base_url','')):
        print('clearing',k,ov); s.set_model_override(k,None)
"
# alternative: edit ~/.hermes/sessions.json and remove matching model_override keys

hermes gateway restart   # run from host shell, not from inside gateway process
```

## OAOS integration guard (this repo — already implemented)

This repo cannot patch Hermes production from inside the gateway. Defense-in-depth here:

- `packages/agent-runtime/agent_runtime/model_guard.py` — `is_blocked_entry` / `sanitize_metadata` / `guard_session_record`; `ALLOWED_PROVIDERS = {claude,codex,gemini,opencode-go,openrouter,ollama}`; blocks `custom`, `gpt-5.6-luna/sol`, `127.0.0.1:10100`.
- `packages/agent-runtime/agent_runtime/session.py` — `SessionManager.create/resume/get_state` sanitize via `sanitize_metadata` / `guard_session_record`.
- `packages/runtime-adapter/runtime_adapter/hermes_adapter.py` & `safe_adapter.py` — `set_model` rejects blocked overrides, `get_model` strips stale, `model_override_guard.py` documents Hermes-parity validation.
- `packages/agent-runtime/agent_runtime/llm_runtime.py` — `LLMProviderAdapter.__init__` + provider_config sanitization.
- `admin-console/backend/fallback.py` — Pydantic validators + `_load_config` strips blocked chain entries before they override routing.
- `tests/test_model_override_guard.py` (22 tests) + `tests/test_fallback_runtime_gate.py` (9 tests) — regression coverage, run with `pytest -q`.

## Verify (repo-only, no production)

```bash
python3 -m pytest tests/test_model_override_guard.py tests/test_fallback_runtime_gate.py -q
python3 -m pytest tests/test_llm_runtime.py -k "not test_stream" -q
ruff check packages/agent-runtime/agent_runtime/model_guard.py packages/runtime-adapter/runtime_adapter/hermes_adapter.py
```

## Commit readiness

- OAOS fix is commit-ready: `packages/agent-runtime/agent_runtime/model_guard.py`, `session.py`, `llm_runtime.py`, `runtime-adapter/*`, `admin-console/backend/fallback.py`, `tests/test_model_override_guard.py`, `docs/HERMES_PRODUCTION_REMEDIATION.md`.
- Hermes remediation is **host-only manual** — not committed here; apply to `/home/openitsvc/.hermes/hermes-agent` separately and restart via host shell (`hermes gateway restart` outside the gateway process).
