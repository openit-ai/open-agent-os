# Process Naming Migration — Control-Plane Centric (Phase 1 Design)

Status: design-fixed. File edits only in Phase 2; no daemon-reload/restart in Phase 1/2.
Date: 2026-09-05. Owner: master approval (stepwise).

## 1. Measured unit inventory (repo `deploy/systemd/`)

| Unit (current) | Description (current) | ExecStart / WorkingDirectory |
|---|---|---|
| `oaos-control-plane.service` | OAOS Control Plane — Mattermost Adapter Gateway :8100 | `uvicorn control_plane.app:app --port 8100`, WD `control-plane` |
| `oaos-execution-gateway.service` | OAOS Execution Gateway :8001 | `uvicorn execution_gateway.app:app --port 8001`, WD `execution-gateway` |
| `oaos-mm-bridge.service` | OAOS Mattermost Bridge — @agent DM/mention ingress to Control Plane | `python3 .../oaos-mm-bridge.py`, WD `/home/openit/apps/oaos/scripts` (prod path; repo has `scripts/`) |
| `oaos-security.service` | OAOS Security & Governance :8002 | WD `security`, app `security/app.py` endpoints `POST /v1/policy/evaluate`, `/v1/delegation/grant`, `/v1/token/issue`, `/v1/approval/request`, `/v1/audit/verify` (Section 7.3) |
| `oaos-admin-api.service` | OAOS Admin API — backend :8010 | WD `.../admin-console/backend` |
| `oaos-admin-console.service` | OAOS Admin Console — Next.js UI :3012 | WD `.../admin-console` |
| `hermes.service` | (external runtime, not OAOS) | Hermes Gateway |

Finding: `oaos-mm-bridge` hides that it is the Mattermost Adapter ingress; `oaos-security` hides policy/delegation/token/approval/audit governance role; CP description says "Mattermost Adapter Gateway" which understates its center role (identity/session/ACP/policy/audit + RuntimeRouter).

## 2. Rename proposal (Alias coexistence, 1 release)

| Current (keep) | New canonical | Mechanism |
|---|---|---|
| `oaos-mm-bridge.service` | `oaos-adapter-mattermost.service` | new file + `Alias=oaos-mm-bridge.service`; old file kept 1 release, then removed with approval |
| `oaos-security.service` | `oaos-governance.service` (alt: `oaos-policy-audit.service`) | same Alias pattern; Description lists policy/delegation/token/approval/audit; decision needed on final noun (see §4) |
| `oaos-control-plane.service` | keep name | Description -> `OAOS Control Plane :8100 — identity/session/ACP adapter/policy/audit + RuntimeRouter (center)` |
| `oaos-execution-gateway.service` | keep name | Description -> `OAOS Execution Gateway :8001 — MCP registry/connectors/capability/risk (tools plane)` |
| `oaos-admin-api/console` | keep names | Description notes Admin plane `:8010/:3012`, not data plane |
| `hermes.service` | keep, external | Docs/UI label as external runtime (`hermes-managed`), never as OAOS unit |

Rules: never rename by delete; `Alias=` + old unit retained; `install-systemd.sh` updated to install both; prod rollout is backup -> change -> `daemon-reload` -> restart each unit with separate confirmation (restart gate applies).

## 3. Backend/API cross-reference

No URL breakage. Additive aliases only (see admin-ia doc §4). Snapshot keeps keys; adds `process_aliases: {mattermost_adapter: {canonical, aliases}, governance: {...}}` as reference-only (no secrets). Rollback = remove alias routes/units, snapshot readers ignore unknown fields.

## 4. Open decisions (master-confirmed 2026-09-05)

1. Governance noun: `oaos-governance` CONFIRMED (matches `security/` code + Section 7.3 endpoints).
2. Bridge noun: `oaos-adapter-mattermost` CONFIRMED (matches `adapters/mattermost/`).
3. Phase 2 order: UI aliases first CONFIRMED (local repo only, no restart).

## 5. Phase 3 implementation record (local repo, 2026-09-05, no restart)

- New canonical units: `deploy/systemd/oaos-adapter-mattermost.service`, `deploy/systemd/oaos-governance.service` (old units kept + successor NOTE comment).
- CP/EG Description updated to center-role wording.
- `install-systemd.sh`: installs + enables canonical units alongside old (old kept 1 release); `bash -n` PASS.
- Backend aliases: `app.py::_mount_router_alias` mounts `/v1/control/acp/*` (3 routes) + `/v1/execution/mcp/*` (5 routes), canonical kept, `include_in_schema=False`.
- Snapshot: `_build_snapshot()` adds reference-only `process_aliases` (no secrets).
- Backup: `/tmp/oaos-phase3-20260905_130100`. Prod rollout + push need separate approval.

## 6. Operational gate (binding)

- Phase 2 edits local repo only; `systemctl daemon-reload`/restart absolutely require separate `예/아니오` confirmation per restart gate (Telegram master DM + text fallback).
- Prod order when approved: backup units + `/etc/oaos/oaos.env` presence/length check (no secret output) -> install alias units -> `daemon-reload` -> restart one unit -> `MainPID`/`/health`/`/readyz`/OpenAPI read-back -> next unit. Hermes Gateway never restarted for OAOS work.
- Verification: `python3 -m py_compile` backend files; `pytest -q` target subset (`test_admin_setup_acp_mcp.py`, runtime-config tests); `bash -n install-systemd.sh`; old + new unit names both resolve.
