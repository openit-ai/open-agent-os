# Vault Externalization Design — Phase A

> Status: **Optional future hardening — not required for the current OAOS deployment**
> Current policy: the existing `encrypted_postgres` backend is the default Secret Vault for the current deployment.
> Scope: preserve an optional migration path to an external backend when customer security, KMS/HSM, or multi-service requirements justify it.
> Depends on: `security/credential-vault/vault/vault.py` (`EncryptedPostgresVault`), `security/models/orm.py` (`VaultCredentialORM`), `alembic/versions/001_initial_persistence.py`, `docs/architecture-v1.6.md` §10.2 + §27.3 + §44  
> Last updated: 2026-09-01

---

## 1. Purpose

Move the Personal Credential Vault from **"encrypted column in Postgres"** to **"external secret backend; Postgres stores `secret_ref` + metadata only"** so that:

- `oaos.vault_credentials` contains no recoverable secret material (not even ciphertext whose key lives on the same host).
- §27.3 "Secret 원문은 Credential Vault에 저장하고 DB에는 `secret_ref`만 저장" / §10.2 "encrypted secret store, plaintext DB 저장 금지" is satisfied literally, not just "encrypted so it's okay".
- Dump / replica / backup of `oaos` alone cannot yield credentials.

Phase A is the design and the migration contract. Code changes land in Phase B.

---

## 2. Current State & Gap

> **Current decision (2026-09-01):** For the current OAOS deployment, `encrypted_postgres` is the accepted and default Secret Vault. The external-backend design below is retained as an optional future hardening path, not as a release blocker.

### 2.1 What exists today

| Component | File | Behavior |
|-----------|------|----------|
| `CredentialVault` ABC + `EncryptedPostgresVault` | `security/credential-vault/vault/vault.py` | `Fernet(sha256(encryption_key))` encrypt, `store()` → `secret_<12hex>`, owner `agent:assistant:<suffix>`, `PERSONAL_CREDENTIAL_USE` audit, `retrieve()` owner-check, `revoke()` delete |
| `VaultCredentialORM` | `security/models/orm.py:133-144` | `secret_ref PK, user_id, owner_agent_id, provider, scope, encrypted_token LargeBinary, created_at` |
| `CredentialBindingORM` | `security/models/orm.py:51-64` | `id PK, delegation_id FK, provider, secret_ref unique, scope, status, expires_at, last_used_at` |
| Alembic 001 | `alembic/versions/001_initial_persistence.py:126-137` | Creates `vault_credentials` exactly as above |
| Config stub | `.env.example:10-11` | `VAULT_BACKEND=encrypted_postgres # or hashicorp_vault / aws_secrets` — referenced but not branched in code |

Dual-mode is already present: if `session_maker is None`, `EncryptedPostgresVault` falls back to in-memory dict (`_store`/`_meta`) for tests.

### 2.2 Why this violates §27.3 / §10.2 intent

Strict reading of §27.3 / §44 decision 36:

> `credential_bindings`에는 실제 secret이 아니라 metadata와 `secret_ref`만 저장한다.  
> DB에 저장 가능한 정보: `provider, client_id, scope, status, enabled, secret_ref, last_rotated_at, expires_at`  
> DB 평문 저장 금지: `client_secret, refresh_token, API key, private key, signing secret, session signing key`  
> Secret 원문은 Credential Vault에 저장하고 `oaos`에는 참조값과 상태 metadata만 저장.

`VaultCredentialORM.encrypted_token` is ciphertext, not plaintext — so it passes a narrow "no plaintext column" check. But it still violates the **intent**:

1. **Ciphertext in the same DB as the key material's host.** `VAULT_ENCRYPTION_KEY` is an env var on the same host that owns `DATABASE_URL`. A single host + DB dump compromise recovers tokens (decrypt with the key from the same backup/config repo).
2. **Blast radius.** Postgres backup, replica, log streaming, and `pg_dump` all carry secret material. §27.3's separation goal ("Credential Vault의 암호화된 secret 경계에서 관리") expects the DB backup to be useless for credential recovery.
3. **Key rotation is painful.** Fernet key rotation requires re-encrypting every row in place; external KMS/HSM rotates keys without touching rows.
4. **Audit §40 expectation.** `Secret Plaintext Persistence → DENY / secret_ref only` would correctly pass today, but `Secret Ciphertext Persistence` is not tested and would silently retain recoverable secrets in DB.

**Therefore the gap is:** `vault_credentials.encrypted_token` must cease to exist. The DB must hold only `secret_ref` + non-secret metadata; secret bytes must live exclusively in an external backend.

---

## 3. Target Architecture

### 3.1 Invariant

```
oaos DB  →  secret_ref + user_id + owner_agent_id + provider + scope + timestamps + status
External Vault  →  secret bytes (token / refresh_token / client_secret / private key / API key)
Never          →  secret bytes (plaintext or ciphertext with DB-co-located key) in oaos
```

`credential_bindings.secret_ref` already satisfies this. `vault_credentials` must be reduced to the same shape (or replaced by the external vault's own storage).

### 3.2 Logical components

```
                 ┌─────────────────────────────────────┐
                 │  Security Core / Credential Vault   │
User ──grant──►  │  CredentialVault (interface)        │
                 │    ├─ store(user, provider, scope, token) → secret_ref
                 │    ├─ retrieve(secret_ref, requester_agent) → bytes
                 │    └─ revoke(secret_ref)
                 │              │               │
                 │              ▼               ▼
                 │     ┌─────────────┐  ┌─────────────────┐
                 │     │  DB (open-  │  │ External Secret │
                 │     │  agentos)   │  │ Backend         │
                 │     │  secret_ref │  │ kv/<secret_ref> │
                 │     │  + metadata │  │ (encrypted at   │
                 │     │  only       │  │  rest by KMS)   │
                 │     └─────────────┘  └─────────────────┘
                 └─────────────────────────────────────┘
```

The `CredentialVault` interface is unchanged for callers (`security/credential-vault/vault/vault.py:26-39`). Only the backend behind it changes.

### 3.3 Backend options (Phase A supports all three; operator picks one)

| `VAULT_BACKEND` value | Backend | Secret-at-rest | Key management | When to use |
|------------------------|---------|----------------|----------------|-------------|
| `encrypted_postgres` (current default for OAOS; optional external migration later) | `oaos.vault_credentials.encrypted_token` via Fernet | Fernet key in `VAULT_ENCRYPTION_KEY` | App-managed | Current deployment default |
| `hashicorp_vault` | HashiCorp Vault KV v2 at `VAULT_ADDR` | Vault transit + storage backend | Vault-managed (auto-rotation, HSM optional) | Enterprise default — aligns with §10 "encrypted secret store" |
| `aws_secrets` (alias: `aws_secrets_manager`) | AWS Secrets Manager (or `aws_kms` + `VAULT_KMS_KEY_ID`) | KMS envelope encryption | AWS KMS / CloudHSM | Customer AWS footprint |

Future: `gcp_secret_manager`, `azure_key_vault` — same interface, new enum value.

All backends must expose identical `store / retrieve / revoke / health_check` semantics. Backend selection is **deployment-time**, not per-credential.

### 3.4 DB shape after externalization

**Option A (recommended): keep `vault_credentials` as metadata-only.**

```python
class VaultCredentialORM(Base):
    __tablename__ = "vault_credentials"
    secret_ref: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    owner_agent_id: Mapped[str] = mapped_column(String(256), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # REMOVED: encrypted_token LargeBinary
    # ADDED (optional, for observability without leaking secrets):
    vault_backend: Mapped[str] = mapped_column(String(32), nullable=False, default="hashicorp_vault")
    vault_path: Mapped[str | None] = mapped_column(String(512), nullable=True)  # e.g. secret/data/openagentos/<secret_ref>
    version: Mapped[int] = mapped_column(nullable=False, default=1)  # KV version for rotation tracking
```

Alternative Option B: drop `vault_credentials` entirely and rely on `credential_bindings` + external vault. Keep Option A because:
- `vault_credentials` is the natural audit-friendly registry of active `secret_ref`s even after externalization.
- `credential_bindings` is scoped to delegations; vault metadata should outlive binding lifecycle for revoke auditing.

**`credential_bindings` is unchanged** — it already stores `secret_ref` only.

### 3.5 Secret reference format

Keep current `secret_<12hex>` for backward compat. Path mapping:

```
HashiCorp Vault: secret/data/openagentos/<secret_ref>   (KV v2)
AWS Secrets Manager: openagentos/<secret_ref>
In-memory / encrypted_postgres (legacy): dict key = secret_ref
```

`secret_ref` remains opaque to callers; no provider/scope encoding inside the ref.

### 3.6 Interface change (backward-compatible)

```python
class CredentialVault(ABC):
    async def store(self, user_id: str, provider: str, scope: str, token: bytes) -> str: ...
    async def retrieve(self, secret_ref: str, requester_agent_id: str) -> bytes: ...
    async def revoke(self, secret_ref: str) -> None: ...
    # New (Phase B):
    async def health_check(self) -> bool: ...  # backend reachable + auth valid
    def backend_name(self) -> str: ...         # for audit / admin UI
```

Existing callers (`security/delegation/delegation_service/service.py:104-117 bind_credential`, `adapters/google/adapter.py:555`, `security/app.py` delegation endpoints) require **no changes**.

Internal dispatch:

```python
def create_vault(session_maker=None, audit_ledger=None, delegation_service=None) -> CredentialVault:
    backend = os.getenv("VAULT_BACKEND", "encrypted_postgres").strip().lower()
    if backend in ("hashicorp_vault", "vault"):
        return HashiCorpVaultBackend(session_maker=session_maker, ...)
    if backend in ("aws_secrets", "aws_secrets_manager", "aws_kms"):
        return AwsSecretsVaultBackend(session_maker=session_maker, ...)
    return EncryptedPostgresVault(...)  # legacy, emits DeprecationWarning
```

Factory lives in `security/credential-vault/vault/__init__.py` (currently empty — to be populated).

---

## 4. Configuration

### 4.1 Environment variables

| Variable | Required for | Default | Example |
|----------|--------------|---------|---------|
| `VAULT_BACKEND` | all | `encrypted_postgres` (current default) | `hashicorp_vault` or `aws_secrets` only when separately approved |
| `VAULT_ENCRYPTION_KEY` | `encrypted_postgres` only | — | `change-me-32-byte-base64==` (32 bytes raw → sha256 → Fernet) |
| `VAULT_ADDR` | `hashicorp_vault` | — | `https://vault.customer.internal:8200` |
| `VAULT_TOKEN` | `hashicorp_vault` (token auth) | — | `hvs.xxx` — prefer AppRole / K8s auth in prod |
| `VAULT_ROLE_ID` / `VAULT_SECRET_ID` | `hashicorp_vault` (AppRole) | — | — |
| `VAULT_KV_MOUNT` | `hashicorp_vault` | `secret` | `secret` |
| `VAULT_KV_PREFIX` | `hashicorp_vault` | `openagentos/` | `openagentos/` |
| `VAULT_NAMESPACE` | `hashicorp_vault` (Enterprise) | — | `admin/` |
| `VAULT_TLS_CA_BUNDLE` | `hashicorp_vault` (mTLS/TLS) | system CA | `/etc/ssl/certs/ca-bundle.crt` |
| `AWS_REGION` | `aws_secrets` | — | `ap-northeast-2` |
| `AWS_SECRETS_PREFIX` | `aws_secrets` | `openagentos/` | `openagentos/` |
| `VAULT_KMS_KEY_ID` | `aws_kms` (envelope path) | — | `arn:aws:kms:…:key/xxx` or alias |
| `VAULT_DUAL_WRITE` | migration only | `false` | `true` during Phase B migration |
| `VAULT_READ_FALLBACK` | migration only | `false` | `true` during migration |

All `VAULT_*` values are **never** returned by any API and never written to `oaos` DB. They are sourced from env / mounted secret file / K8s Secret, consistent with §27.3 "DB password는 config 파일 평문 하드코딩 금지".

### 4.2 `.env.example` delta

```diff
 # --- Vault (choose one) ---
-VAULT_BACKEND=encrypted_postgres  # or hashicorp_vault / aws_secrets
-VAULT_ENCRYPTION_KEY=change-me-32-byte-base64==
+# Legacy (deprecated, kept for rollback only):
+# VAULT_BACKEND=encrypted_postgres
+# VAULT_ENCRYPTION_KEY=change-me-32-byte-base64==
+
+# Recommended:
+VAULT_BACKEND=hashicorp_vault
+VAULT_ADDR=https://vault.internal:8200
+VAULT_KV_MOUNT=secret
+VAULT_KV_PREFIX=openagentos/
+# Auth: either VAULT_TOKEN (dev) or VAULT_ROLE_ID + VAULT_SECRET_ID (prod)
+
+# Alternative: AWS
+# VAULT_BACKEND=aws_secrets
+# AWS_REGION=ap-northeast-2
+# AWS_SECRETS_PREFIX=openagentos/
```

### 4.3 Runtime wiring

- `security/models/db.py:get_engine()` / `get_sessionmaker()` unchanged.
- Vault factory called from FastAPI lifespan (`security/app.py`, `control-plane/control_plane/app.py`) where `session_maker` is injected — same pattern as `EncryptedPostgresVault.set_session_maker()` today.
- Health check endpoint: `GET /v1/vault/health` → `vault.health_check()` (already partially implied by `security/app.py`).

---

## 5. Migration Plan (Zero-Downtime, Reversible)

### 5.1 Principles

- No secret bytes in logs, error messages, or audit payloads — only `secret_ref`, `provider`, `user_id` hash.
- Reversible at every step; rollback is `VAULT_BACKEND=encrypted_postgres` + restore.
- Owner-check and `PERSONAL_CREDENTIAL_USE` audit remain enforced by `CredentialVault` regardless of backend (never delegated to Vault ACL alone).
- Refresh / access token rotation continues via `scope` separation; migration does not change token semantics.

### 5.2 Phases

#### Phase 0 — Pre-flight (1-2 days)

1. Inventory: `SELECT count(*), provider FROM vault_credentials GROUP BY provider` + `SELECT count(*) FROM credential_bindings WHERE secret_ref NOT IN (SELECT secret_ref FROM vault_credentials)` (orphan check).
2. Backup: `pg_dump oaos` + snapshot external vault (if already in use elsewhere).
3. Add feature flag columns (no data move yet):
   - Alembic `003_vault_externalization_prep.py`: add `vault_backend`, `vault_path`, `version` to `vault_credentials` (nullable, default `encrypted_postgres`), add `CHECK (vault_backend IN (...))`.
4. Deploy code with **dual-read support only** (no dual-write) behind flag — no behavior change.

#### Phase 1 — Dual-write (3-7 days, operator-controlled)

Set `VAULT_BACKEND=hashicorp_vault`, `VAULT_DUAL_WRITE=true`, `VAULT_READ_FALLBACK=true`.

New `store()` behavior (only when Phase B code ships; described here for design completeness):

```
store(user, provider, scope, token):
  1. generate secret_ref
  2. write to external vault: PUT secret/data/openagentos/<ref> { token: <base64>, user_id, provider, scope }
     - if external write fails → abort, do NOT write DB row, return 5xx (never leave orphan DB row)
  3. write DB metadata row (no encrypted_token): (secret_ref, user_id, owner_agent_id, provider, scope, created_at, vault_backend='hashicorp_vault', vault_path='secret/data/openagentos/<ref>')
  4. if VAULT_DUAL_WRITE: also write legacy encrypted_token row (for rollback window) — best-effort, log on failure
  5. return secret_ref
```

New `retrieve()`:

```
retrieve(secret_ref, requester):
  1. load DB metadata row (for owner_agent_id check)
  2. if owner mismatch → PermissionError (before touching external vault)
  3. try external vault GET
  4. if not found and VAULT_READ_FALLBACK: try legacy vault_credentials.encrypted_token decrypt (covers not-yet-migrated rows)
  5. on success → audit PERSONAL_CREDENTIAL_USE, return bytes
```

`revoke()` deletes from **both** external vault and DB (and legacy column if dual-write).

#### Phase 2 — Backfill / Re-encrypt (bulk migration of existing rows)

Offline or online job — operator chooses:

**Online (recommended for <100k rows):**

```python
# scripts/migrate_vault_to_external.py  (to be added in Phase B)
async for row in SELECT * FROM vault_credentials WHERE vault_backend='encrypted_postgres':
    plaintext = fernet.decrypt(row.encrypted_token)  # using VAULT_ENCRYPTION_KEY
    await external_vault.put(row.secret_ref, plaintext, metadata=row)
    UPDATE vault_credentials SET vault_backend='hashicorp_vault',
                                 vault_path='secret/data/openagentos/<ref>',
                                 version=1
    WHERE secret_ref=row.secret_ref
    # optionally: UPDATE vault_credentials SET encrypted_token=NULL  (after verification)
```

Requirements:
- Batch size 100-500, with `SELECT ... FOR UPDATE SKIP LOCKED` for concurrency safety.
- Rate-limit external vault writes (Vault `max_requests` / AWS `ThrottlingException` backoff).
- Emit audit `VAULT_MIGRATION_WRITE` per batch (count only, no secret).
- Idempotent: re-running on already-migrated `secret_ref` is a no-op (check `vault_backend`).

**Offline alternative:** dump `encrypted_token` locally, re-encrypt to external vault from a hardened migration host — same logic, no live DB pressure.

#### Phase 3 — Verification

1. **Row-count parity:** `SELECT count(*) FROM vault_credentials WHERE vault_backend='hashicorp_vault'` == `external_vault list | wc -l` == pre-migration `count(*)`.
2. **Spot-check decrypt (sample 1%):**
   ```sql
   SELECT secret_ref FROM vault_credentials TABLESAMPLE SYSTEM(1) LIMIT 100;
   ```
   For each `ref`, `retrieve(ref, owner)` via new backend == `fernet.decrypt(legacy_backup.encrypted_token)` (compare on migration host, never log plaintext).
3. **No secret in DB dump:**
   ```bash
   pg_dump --table=vault_credentials oaos | strings | grep -v secret_ref | wc -l  # no LargeBinary column
   # and:
   psql -c "\d vault_credentials" | grep -qi "encrypted_token" && echo FAIL || echo PASS
   ```
4. **Fallback probe:** set `VAULT_READ_FALLBACK=false` in staging, confirm all active `credential_bindings.secret_ref` still resolve.
5. **Audit coverage:** every `retrieve` after cutover emits `PERSONAL_CREDENTIAL_USE` with `vault_backend` label; no gaps.

#### Phase 4 — Cutover & Cleanup

1. Set `VAULT_DUAL_WRITE=false`, `VAULT_READ_FALLBACK=false` (single-writer to external vault).
2. Alembic `004_vault_drop_encrypted_token.py`:
   ```python
   op.drop_column("vault_credentials", "encrypted_token")  # or: ALTER COLUMN SET NOT NULL removal then drop
   # keep vault_backend / vault_path / version NOT NULL from now on
   ```
3. Remove `VAULT_ENCRYPTION_KEY` from deployed env (keep in sealed backup for disaster recovery of old dumps only).
4. Update runbook: `pg_dump oaos` is now safe to handle without secret-handling caveats; external vault backup becomes the secret backup.

#### Rollback at any point

- Before Phase 4: set `VAULT_BACKEND=encrypted_postgres`, `VAULT_DUAL_WRITE` off, restart — legacy rows still present; new rows written during dual-write exist in both places.
- After Phase 4 (`encrypted_token` column dropped): restore `pg_dump` from Phase 0 + re-import external vault dump into `encrypted_token` via reverse script — documented but expected to be rare; operator must retain Phase 0 dump until Phase 4 is declared stable (≥7 days).

### 5.3 Alembic sequence summary

| Revision | Purpose | DDL |
|----------|---------|-----|
| `003_vault_externalization_prep` | Add metadata cols, no drop | `ADD COLUMN vault_backend, vault_path, version` |
| `004_vault_drop_encrypted_token` | After verification | `DROP COLUMN encrypted_token`, `SET NOT NULL vault_backend` |

Both revisions are `downgrade()`-able to full `encrypted_token` restoration from backup (not from live data after drop).

---

## 6. Security Considerations

| Concern | Mitigation |
|---------|------------|
| External vault becomes single point of failure | `health_check()` on startup + readiness probe; `retrieve` failure → 503 not plaintext fallback; no silent DB fallback after cutover |
| Vault token leakage | `VAULT_TOKEN` never in DB or audit; AppRole/K8s auth preferred; token rotation via vault agent sidecar |
| Network eavesdropping | `VAULT_ADDR` is `https://` only; `VAULT_TLS_CA_BUNDLE` pinned; AWS via IAM role not static key |
| Blast radius (Hermes) | Unchanged: Hermes never gets external vault creds; only `Security Core` → vault, Hermes → `CredentialVault.retrieve()` with owner check |
| Backup separation | DB backup no longer contains secrets; vault backup is separate, encrypted by vault's own KMS/HSM — satisfies §27.3 separation |
| Key rotation | External vault rotates KMS/master key without row rewrites; `version` column tracks KV version for audit |
| Revocation immediacy | `revoke()` deletes external key + DB row synchronously; no async eventual consistency |
| Tenant isolation | `vault_path` prefix per tenant (`openagentos/<tenant_id>/<ref>`) when multi-tenant mode is enabled (§27.4 namespace) |

---

## 7. Test Plan (Phase A — no code changes, test scaffolding only)

### 7.1 Tests that must pass *before* migration code is written (baseline)

| Test file | Case | Assertion |
|-----------|------|-----------|
| `tests/test_vault_externalization_gap.py` (new) | `test_vault_credentials_still_has_encrypted_token_column` | Fails today (column exists) → passes after Phase 4 (column gone) — documents gap |
| `tests/test_vault_owner_isolation.py` (existing pattern) | `retrieve` with wrong `requester_agent_id` | `PermissionError` regardless of backend |

### 7.2 Unit tests (Phase B, mocked backends — no real Vault/AWS required)

| Test | Backend | Mock | Assertions |
|------|---------|------|------------|
| `test_store_creates_secret_ref_and_no_db_ciphertext` | `hashicorp_vault` | `hvac` / `unittest.mock` | `store()` returns `secret_…`, DB row has `vault_backend='hashicorp_vault'`, `encrypted_token` is `None`/`AttributeError`, external `put` called once with correct path |
| `test_retrieve_owner_check_before_external_call` | `hashicorp_vault` | mock | Wrong agent → `PermissionError`, external `get` **not** called |
| `test_retrieve_success_audits` | all | mock | `PERSONAL_CREDENTIAL_USE` event appended with `provider`, `secret_ref`, no secret in event |
| `test_revoke_deletes_both` | `hashicorp_vault` | mock | `revoke()` calls external `delete` + DB `DELETE`, subsequent `retrieve` → `KeyError` |
| `test_dual_write_writes_both` | `hashicorp_vault` + `VAULT_DUAL_WRITE=true` | mock | External `put` + legacy `encrypted_token` row both present |
| `test_read_fallback_for_legacy_row` | `hashicorp_vault` + `VAULT_READ_FALLBACK=true` | mock external 404 | Falls back to legacy decrypt, succeeds |
| `test_health_check` | `hashicorp_vault` / `aws_secrets` | mock | `health_check()` returns bool, startup fails closed if `false` |
| `test_factory_selects_backend_from_env` | — | `monkeypatch VAULT_BACKEND` | `create_vault()` returns correct subclass (`EncryptedPostgresVault` / `HashiCorpVaultBackend` / `AwsSecretsVaultBackend`) |
| `test_aws_backend_path_and_region` | `aws_secrets` | `moto` | Secret name `openagentos/<ref>` in `AWS_REGION`, tag `provider/scope` |
| `test_no_secret_in_logs_or_audit` | all | `caplog` | `store/retrieve/revoke` never log plaintext bytes |

### 7.3 Integration tests (require Docker / Vault dev server)

```yaml
# docker-compose.test.yml fragment
services:
  vault:
    image: hashicorp/vault:1.15
    environment: { VAULT_DEV_ROOT_TOKEN_ID: root, VAULT_DEV_LISTEN_ADDRESS: "0.0.0.0:8200" }
    ports: ["8200:8200"]
  postgres:
    image: pgvector/pgvector:pg16
```

| Test | Setup | Steps |
|------|-------|-------|
| `test_integration_hashicorp_roundtrip` | Vault dev server + real Postgres | `store` → `retrieve` → assert bytes equal; `revoke` → `retrieve` 404 |
| `test_integration_migration_script_idempotent` | Pre-seeded `encrypted_token` rows + Vault dev | Run `scripts/migrate_vault_to_external.py` twice → second run no-ops, row counts stable |
| `test_integration_no_ciphertext_after_migration` | After migration | `psql \d vault_credentials` has no `encrypted_token`, `pg_dump` contains no `LargeBinary` |

### 7.4 Negative / failure tests

| Case | Expected |
|------|----------|
| External vault unreachable on `store` | `store` raises, no DB row created (atomicity) |
| External vault 404 on `retrieve` with fallback off | `KeyError` |
| Vault token expired / IAM denied | `health_check()==False`, readiness probe fails, pod not routed |
| `VAULT_BACKEND` unknown value | Startup raises `ValueError: unknown VAULT_BACKEND` — fail closed |

### 7.5 Coverage gates

- `security/credential-vault/vault/` line coverage ≥ 90% with mocked backends.
- Migration script covered by at least one idempotent integration run (CI with Vault dev sidecar).
- No test ever asserts on or logs plaintext secret bytes — grep CI for `token`, `refresh_token` in test output and fail if found outside `b"fake-token"` fixtures.

---

## 8. Rollout Checklist

- [ ] This design reviewed and approved (Security + Platform).
- [ ] `003_vault_externalization_prep` Alembic migration applied to staging.
- [ ] Phase B code: `HashiCorpVaultBackend`, `AwsSecretsVaultBackend`, factory, dual-write/fallback flags, `scripts/migrate_vault_to_external.py`.
- [ ] Unit tests (mocked) green in CI.
- [ ] Integration tests with Vault dev server green in CI.
- [ ] Staging dual-write soak (≥48h) with `VAULT_DUAL_WRITE=true` + `VAULT_READ_FALLBACK=true`.
- [ ] Backfill job run on staging, verification queries pass.
- [ ] Cutover staging to `VAULT_DUAL_WRITE=false` + `VAULT_READ_FALLBACK=false`, re-verify.
- [ ] Production pre-flight (inventory + backup), then repeat phased rollout.
- [ ] `004_vault_drop_encrypted_token` applied, `VAULT_ENCRYPTION_KEY` removed from prod env.
- [ ] Docs updated: `docs/deployment.md`, `deploy/k8s/managed-values.yaml`, `SECURITY.md` vault section.

---

## 9. Out of Scope (Phase A)

- Changing `CredentialVault` caller API (delegation, adapters) — interface is stable.
- Encrypting non-vault DB columns — tracked separately.
- Multi-region vault replication / DR — operator concern, not Phase A.
- Automatic secret rotation (refresh token rotation) — existing vault already handles this via re-`store`.

---

## 10. References

- `docs/architecture-v1.6.md` §10 (Personal Credential Vault), §10.2 (보안 원칙), §27.3 (Admin Web UI Persistence & Basic Security — Secret 저장 원칙), §40 (Secret Plaintext Persistence check), §44 decisions 36/52.
- `security/credential-vault/vault/vault.py` — current `EncryptedPostgresVault` (Fernet, owner check, audit).
- `security/models/orm.py` — `VaultCredentialORM` / `CredentialBindingORM`.
- `alembic/versions/001_initial_persistence.py` — `vault_credentials` DDL.
- `.env.example` — `VAULT_BACKEND` / `VAULT_ENCRYPTION_KEY` stub.
- `GAP_AUDIT_CORE_PLATFORM_v1.6.md` — gap audit noting vault is §10-compliant but with dual-mode DB/in-memory and missing explicit `issued_at/refreshable`.

---

*This document is design-only. No vault code is modified in Phase A. Phase B implements the backends, factory, dual-write, migration script, and tests described above.*
