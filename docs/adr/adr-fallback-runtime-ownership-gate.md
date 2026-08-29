# ADR: Fallback Runtime Ownership Gate (Hermes vs OAOS LLM Runtime)

- Date: 2026-08-29
- Status: Implemented
- Owners: Admin Console + Runtime Adapter
- Related: §16.1.2 LLM Multi-Provider, §16E Runtime Mode, commit 544e54b fallback feature

## Context
Commercial OAOS server runs **Hermes Runtime** (authoritative). Hermes Agent owns LLM routing
internally. Existing commit `544e54b` added Admin fallback UI/API that optionally wrote
`HERMES_CONFIG_PATH` / `OAOS_HERMES_CONFIG_PATH` JSON — architectural mismatch: OAOS fallback
was presented/written as if it were Hermes Runtime config.

## Decision
- **Ownership gate**: `fallback.py` checks `runtime_mode` before every `/v1/llm/fallback` handler.
  - `runtime_mode==hermes` → `409 {code: HERMES_MODE_NOOP}` on GET/PUT/POST, identical contract to
    `GET/POST/PUT/DELETE /v1/llm/providers*` in `llm_providers.py#700`.
  - `runtime_mode==llm` → full CRUD + DB/env mirroring stays functional (preserved for
    deployments that use OAOS LLM Runtime).
  - Fail-open when `runtime_mode` unavailable (offline/dev) — preserves OAOS.
- **No Hermes write by default**: `_write_hermes_config` is now no-op unless explicit opt-in
  `OAOS_ALLOW_HERMES_FALLBACK_WRITE=1` (or legacy `OAOS_ALLOW_HERMES_CONFIG_WRITE=1`) **and**
  `runtime_mode==llm`. Never writes when Hermes-owned, even with opt-in.
- **Env mirroring scoped**: `OAOS_LLM_FALLBACK_JSON` / `OAOS_FALLBACK_PROVIDERS` /
  `OAOS_FALLBACK_MODEL` remain for OAOS Runtime consumers only — documented as OAOS LLM Runtime
  only, not Hermes.
- **UI gate**: `fallback/page.tsx` fetches `getRuntimeMode()` first. When `hermes`, shows
  ownership banner (`hermesBanner*` i18n) and does not render the chain editor (no Hermes config
  presentation). Also handles 409 → banner.
- **i18n**: `helpNote` / `hermesBanner*` updated in `en.json`/`ko.json`.

## Consequences
- Hermes commercial server: fallback UI shows banner, API returns 409 — no silent Hermes config overwrite.
- OAOS LLM Runtime deployments: unaffected when `OAOS_RUNTIME_MODE=llm`.
- Legacy migration: set `OAOS_ALLOW_HERMES_FALLBACK_WRITE=1` + `runtime_mode=llm` + `HERMES_CONFIG_PATH` to restore mirror temporarily.

## Tests (repo-only, no production)
- `tests/test_fallback_runtime_gate.py` (9 tests): hermes 409s, hermes never writes even with path,
  llm CRUD persists, mirror blocked without allow, mirror allowed with opt-in, validation still enforced,
  helper predicates.
- Existing `tests/test_llm_runtime_providers.py` still passes (13 tests).

## Files
- `admin-console/backend/fallback.py` — gate + Hermes write deprecation + aliases for compat.
- `admin-console/app/(dashboard)/fallback/page.tsx` — runtimeMode fetch + banner.
- `admin-console/lib/i18n/en.json`, `admin-console/lib/i18n/ko.json` — ownership docs.
- `tests/test_fallback_runtime_gate.py` — gate tests.
- `docs/adr/adr-fallback-runtime-ownership-gate.md` — this doc.
