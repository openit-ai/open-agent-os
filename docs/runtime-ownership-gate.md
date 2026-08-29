# Runtime Ownership Gate — Hermes vs OAOS LLM Fallback (v1.7.1+)

Status: implemented · Backend `admin-console/backend/fallback.py` + `admin-console/backend/app.py` + frontend `admin-console/app/(dashboard)/fallback/page.tsx` · Tests `tests/test_fallback_runtime_gate.py`.

## 1. Authority boundary

| Concern | Owner when `runtime_mode=hermes` (commercial server) | Owner when `runtime_mode=llm` |
|---|---|---|
| **Model routing** | **Hermes Runtime** (authoritative, `hermes-agent`) | OAOS LLM Runtime |
| **LLM Multi-Provider CRUD** (`/v1/llm/providers*`, test/toggle) | noop `409 HERMES_MODE_NOOP` — delegated to Hermes | OAOS Admin API (persisted in `admin_llm_providers`) |
| **LLM Fallback chain** (`/v1/llm/fallback`) | **noop `409 HERMES_MODE_NOOP`** — not presented nor written as Hermes config | **OAOS LLM Runtime only** — persisted in `admin_settings.llm_fallback` + `OAOS_LLM_FALLBACK_JSON`/`OAOS_FALLBACK_PROVIDERS` |
| **Hermes config mirroring** (`HERMES_CONFIG_PATH` / `OAOS_HERMES_CONFIG_PATH`) | **blocked** (deprecated) | blocked by default; opt-in only `OAOS_ALLOW_HERMES_CONFIG_WRITE=1` |

Hermes Runtime is authoritative on the commercial server. OAOS does not present or write Hermes-owned fallback/mirror config. OAOS LLM Runtime capability is preserved for deployments that choose it.

## 2. Backend enforcement

`admin-console/backend/fallback.py`

- Helpers:
  ```python
  def _is_hermes_owned() -> bool:  # runtime_mode == hermes
  def _is_hermes_config_mirror_allowed() -> bool:
      # OAOS_ALLOW_HERMES_CONFIG_WRITE in (1/true/yes/on) AND not hermes-owned
  def _write_hermes_config(cfg) -> None:
      # deprecated, logs warning, returns unless _is_hermes_config_mirror_allowed()
  ```
- `GET /v1/llm/fallback`, `PUT /v1/llm/fallback`, `POST /v1/llm/fallback` call `_is_hermes_owned()` first and return:
  ```json
  { "detail": { "code": "HERMES_MODE_NOOP", "message": "Hermes mode is active — LLM Fallback is disabled. ..." } }
  ```
  with HTTP 409 — same code as LLM Providers gate (§16.1.2(8)).
- When `runtime_mode=llm`, normal flow: validate `FallbackUpdateRequest` (max chain 20, model ≤128), save to DB (`admin_settings.llm_fallback`) + mirror `OAOS_LLM_FALLBACK_JSON` / `OAOS_FALLBACK_PROVIDERS` / `OAOS_FALLBACK_MODEL`; `_write_hermes_config` is attempted but no-ops unless opt-in.

Env:

- `OAOS_RUNTIME_MODE` / `runtime_mode` table (`admin_settings.runtime_mode`) — canonical in `admin-console/backend/runtime_mode.py`.
- `OAOS_HERMES_CONFIG_PATH` / `HERMES_CONFIG_PATH` — read but deprecated; writes require `OAOS_ALLOW_HERMES_CONFIG_WRITE=1`.
- `OAOS_LLM_FALLBACK_JSON` / `OAOS_FALLBACK_PROVIDERS` — OAOS LLM Runtime consumers only.

## 3. Frontend enforcement

- `admin-console/app/(dashboard)/fallback/page.tsx`: loads `getRuntimeMode()` alongside `getFallbackConfig()`. When `runtimeMode === 'hermes'` shows ownership banner (`fallback.hermesBanner*`), disables Add/Save/Reorder/Toggle/Delete, and surfaces 409 as banner instead of silent failure. Reads still blocked by API (409) and surfaced.
- `admin-console/app/(dashboard)/providers/page.tsx`: existing `runtimeMode` banner preserved.
- `admin-console/app/(dashboard)/layout.tsx`: `fallback` nav remains (OAOS capability is kept); page itself gates — no silent omission.
- `admin-console/lib/i18n/{en,ko}.json`: `fallback.helpNote` clarified + `fallback.hermesBannerTitle/Desc/Detail/Action` added.

## 4. No production mutation

- Changes are repo-only; `deploy/systemd/`, `deploy/*.yml`, `config/oaos.env` (0600) are untouched.
- Hermes config file is never written when `runtime_mode=hermes`; even with `OAOS_ALLOW_HERMES_CONFIG_WRITE=1` the write is blocked.

## 5. Tests

`tests/test_fallback_runtime_gate.py` (9 cases): hermes 409 for GET/PUT/POST, hermes config never written, llm GET/PUT works, llm mirror blocked without allow, llm mirror allowed with opt-in, validation still enforced (chain>20 →400, bad provider →422), `_is_hermes_owned` / `_is_hermes_config_mirror_allowed` helpers.

Run: `pytest tests/test_fallback_runtime_gate.py -v`.

## 6. Migration / rollback

- No migration: gate is code-only. Existing `admin_settings.llm_fallback` rows remain readable when switching to `runtime_mode=llm`.
- To re-enable Hermes mirroring (not recommended): set `OAOS_RUNTIME_MODE=llm` and `OAOS_ALLOW_HERMES_CONFIG_WRITE=1` and restart admin API.
