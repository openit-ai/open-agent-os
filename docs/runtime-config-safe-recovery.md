# Safe Non-Secret Recovery — Runtime Config (Empty DB)

> Non-secret only. No credentials, no publish/apply, no restart, no DB write.
> Production DB `admin_runtime_config_*` tables are currently empty. This doc gives exact steps to recreate a canonical snapshot after restart via Admin API or UI, using the fixed collector.

## 1. What was fixed (code)

- Collector `admin-console/backend/runtime_config.py::_collect_hermes` now:
  - Prefers effective env `OAOS_CP_HERMES_BASE_URL` / `HERMES_BASE_URL` and `OAOS_CP_HERMES_MODEL` / `HERMES_MODEL`.
  - Canonicalizes `localhost` → `127.0.0.1` and `127.0.0.1:8001` → `127.0.0.1:8642` (canonical OAOS Hermes gateway).
  - Ignores stale `control_plane.config` defaults (`localhost:8001`, `qwen2.5`) unless canonical env present; falls back to `http://127.0.0.1:8642` + `muse-spark-1.2-contributor`.
  - Emits `source` + `observed_at` per collector; `llm_providers` additive provenance (`llm_providers_source/observed_at/inventory_status/count`) without secret raw.
- Same collector runs on next `POST /v1/runtime/config/snapshot` after restart, so any new snapshot is canonical even when DB was empty.
- Destructive guard: `_is_destructive_db_allowed()` (Admin) and `_is_destructive_allowed()` (CP) strictly allow DB wipe only for `sqlite://` isolated URLs and never in `OAOS_ENV=production`, even with `OAOS_ALLOW_DESTRUCTIVE_RUNTIME_CONFIG_CLEAR=1`. Tests patched via `tests/conftest.py::_runtime_config_isolation_guard`.

## 2. Preconditions (read-only)

```bash
# verify tables empty (read-only, no write)
psql "$DATABASE_URL" -c "select count(*) from admin_runtime_config_snapshots; select count(*) from admin_runtime_config_published; select count(*) from admin_runtime_config_applied;"
# or via Admin API
curl -H "Authorization: Bearer <L5>" https://admin.example.com/v1/runtime/config/status?tenant_id=default
# expected: published_version null, has_snapshot false
```

## 3. Exact recovery steps — Admin API (non-secret)

Prereq: env after restart must have canonical Hermes env (already set in production `.env`):
`OAOS_CP_HERMES_BASE_URL=http://127.0.0.1:8642` and `OAOS_CP_HERMES_MODEL=muse-spark-1.2-contributor`.

```bash
# 1) Login as L5
TOKEN=$(curl -s -X POST https://admin.example.com/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@openit.co.kr","password":"<password>"}' | jq -r .access_token)

# 2) Create canonical snapshot (collector runs live, no secret raw)
curl -s -X POST https://admin.example.com/v1/runtime/config/snapshot \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"tenant_id":"default"}' | jq '{version, tenant_id, config: {hermes: .config.hermes, llm_providers_count: .config.llm_providers_count}}'
# Expect: {"version":1,"tenant_id":"default","config":{"hermes":{"base_url":"http://127.0.0.1:8642","model":"muse-spark-1.2-contributor","source":"...","observed_at":"..."}, ...}}

# 3) Verify no raw secret in snapshot
curl -s https://admin.example.com/v1/runtime/config/snapshots/1 -H "Authorization: Bearer $TOKEN" | jq . | grep -i "encrypted_api_key\|api_key" && echo "FAIL: secret leaked" || echo "OK: no secret raw"

# 4) Inspect signature validity (read-only)
curl -s https://admin.example.com/v1/runtime/config/status?tenant_id=default -H "Authorization: Bearer $TOKEN" | jq '{published_version, has_snapshot, signature_valid}'

# 5) Publish ONLY after parent approval gate (do NOT run without approval)
# curl -s -X POST https://admin.example.com/v1/runtime/config/publish -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"tenant_id":"default","version":1}' | jq .

# 6) Control Plane verification (read-only, no apply without approval)
# curl -s https://cp.example.com/v1/runtime-config -H "X-User-Id: employee:alice" -H "X-Tenant-Id: default" | jq '{verified, published_version, config_hash}'

# 7) Apply ONLY after parent approval gate
# curl -s -X POST https://cp.example.com/v1/runtime-config/apply -H "X-User-Id: employee:alice" -H "X-Tenant-Id: default" | jq .
```

## 4. UI steps (Admin Console)

1. Admin Console → Login (L5)
2. Left nav → `Runtime Config` (route `/(dashboard)/runtime-config`)
3. `Create Snapshot` → tenant `default` → confirm
4. Verify drawer shows `hermes.base_url = http://127.0.0.1:8642`, `hermes.model = muse-spark-1.2-contributor`, `source`/`observed_at` present, `llm_providers` entries show `secret_ref: vault://` only (no `encrypted_api_key`)
5. `Snapshots` table → new version `1` → `Publish` button (disabled until parent approval)
6. Status card → `signature_valid: true`, `config_hash: <sha256>` (read-only)
7. After parent approval: `Publish` → then CP `Status` → `Apply` (requires L5/service token)

## 5. Safety invariants

- Do not set `OAOS_ALLOW_DESTRUCTIVE_RUNTIME_CONFIG_CLEAR=1` in production; guard now blocks postgres wipe even if set.
- Tests use `sqlite://` isolated URLs only (`tests/test_runtime_config_guard_regression.py` covers leak scenario).
- Never commit `.env` secrets; recovery file contains no secret-bearing env values.
- Do not run `publish`/`apply`/`restart` without parent gate; this doc is read-only preparation.

## 6. Verification that collector will produce canonical after restart

Unit proof (no DB):
```
pytest tests/test_runtime_config_collector_correction.py::test_hermes_canonicalization_uses_effective_env_and_localhost_normalized -v
pytest tests/test_runtime_config_collector_correction.py::test_hermes_snapshot_config_uses_effective_env_via_api -v
pytest tests/test_runtime_config_guard_regression.py -v
```
All pass with canonical `127.0.0.1:8642` / `muse-spark-1.2-contributor` and `source`/`observed_at` non-`unknown`.

## 7. Preserved artifacts

- Backups: `.backup_runtime_config_20260831_*`, `.backup_infra_live_20260831`, `.e2e-backups`, `.release_backup_0.1.2` untouched.
- Dirty worktree (unrelated modules) preserved; only `admin-console/backend/runtime_config.py`, `control-plane/control_plane/runtime_config.py`, `tests/conftest.py`, `tests/test_runtime_config_guard_regression.py`, `docs/runtime-config-safe-recovery.md` touched.
