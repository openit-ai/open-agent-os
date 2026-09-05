# Admin IA Redefinition — Control-Plane Centric (Phase 1 Design)

Status: design-fixed for stepwise migration. No production change in this doc.
Repo: `openit-ai/open-agent-os`. Date: 2026-09-05. Owner: master approval (stepwise).

## 1. Measured truth (not assumption)

Flow:
`Mattermost/Slack Adapter (oaos-mm-bridge.py) -> Control Plane :8100 (identity/session/ACP adapter/policy/audit) -> RuntimeRouter (Hermes :8642 vs external LLM) -> Execution Gateway :8001 (MCP registry/connectors) -> Knowledge (Outline/Index/Memory) -> Admin :8010/:3012`

Evidence:
- `control-plane/control_plane/acp_adapter.py`: ACP is translation layer, fallback to `/v1/chat/completions` when `/acp/sessions` 404.
- `adapters/hermes/adapter.py`: real transport is `HERMES_BASE_URL` Gateway API.
- `admin-console/backend/acp_config.py` + `app/(dashboard)/providers/acp-section.tsx`: ACP buried under Providers; DB save returns `applied=false` until CP restart (live CP reads `OAOS_CP_*` env at startup).
- `execution-gateway/execution_gateway/mcp_registry.py`: discovery only; policy in PolicyEngine/authz_hook.
- `packages/agent-runtime/agent_runtime/mcp_client.py`: proxies via gateway.
- `admin-console/backend/mcp_config.py` + `app/(dashboard)/infra/mcp-panel.tsx`: MCP buried as 5th tab of Infra.
- `admin-console/backend/runtime_config.py`: `_build_snapshot()` collects `runtime_mode/hermes/llm_providers/fallback/infra/user_mappings/observed_inventory`; no ACP/MCP alias fields yet.
- `admin-console/backend/app.py`: routers registered flat (`auth/infra/business/managed/user_mappings/llm_providers/runtime_mode/fallback/setup/acp/mcp/mm/ol/notion/slack/oauth/smtp/quota/embedding/...`).
- Nav: `app/(dashboard)/layout.tsx` `navItems` 20 flat entries; i18n `lib/i18n/ko.json`, `en.json`.

## 2. Target IA — 6 groups (Control-Plane centric)

Keep every existing URL. Add group aliases; no route deletion in Phase 1.

1. Ingress (연결): channels — Mattermost / Slack / Notion / OAuth / SMTP
   - sources: `infra/mm-panel, slack-panel, notion-panel, oauth-panel, smtp-panel` + `mattermost_config/slack_config/notion_config/oauth_config/smtp_config.py`
2. Control Plane (제어): identity/session/ACP runtime/policy/approvals/audit
   - ACP promoted from Providers subsection to first-class section here
   - sources: `providers/acp-section.tsx` + `acp_config.py` + `runtime_config.py` + `policy/approvals/audit/users`
3. Execution (실행): gateway/MCP/LLM providers/fallback/quota/usage
   - MCP promoted from Infra tab to first-class section here
   - sources: `infra/mcp-panel.tsx` + `mcp_config.py` + `providers/page.tsx` + `fallback/quota/llm-usage`
4. Knowledge (지식): Outline/Wiki/Memory/Embedding/knowledge-ops
   - sources: `infra/ol-panel.tsx` + `outline_config.py` + `knowledge-ops/embedding`
5. Operations (운영): infra health/unified/live + backup + security-updates + license
   - sources: `infra/page.tsx` services/live/unified/setup tabs
6. Management (관리): users/credentials/secrets/feature-flags + setup wizard
   - sources: `users/credentials/secrets/feature-flags/setup`

Sidebar: group headers + existing links nested underneath (flat list preserved for mobile drawer + bookmarks). Financial palette only: ok #22C55E / warn #F59E0B / danger #DC2626. No purple/pink gradients. SVG icons only. WCAG AA contrast, visible focus, hover 150-300ms, `prefers-reduced-motion` respected.

## 3. Route alias table (Phase 1 — additive only)

| New (group view) | Existing (keep) | Type |
|---|---|---|
| `/control/acp` | `/providers`#acp | alias/redirect view of `AcpSection` |
| `/execution/mcp` | `/infra`#mcp | alias view of `McpPanel` |
| `/control/runtime` | `/runtime-config` | alias (same component) |
| `/ingress/*` | `/infra` tabs mm/slack/notion/oauth/smtp | alias views |
| `/knowledge/*` | `/knowledge-ops`, `/embedding`, infra ol tab | alias views |

Rules: old URLs never break; `/setup` -> `/infra` redirect stays; i18n adds `nav.groups.*` keys, never renames existing `nav.*` in Phase 1 (rename in Phase 2 with Outline sync).

## 4. Backend API alias (additive, Phase 2 implementation)

Keep: `GET/PUT /v1/acp/config`, `POST /v1/acp/test`, `GET/POST /v1/mcp/servers`, etc.
Add: `GET /v1/control/acp/config` -> same handler as `/v1/acp/config`; `GET /v1/execution/mcp/servers` -> same as `/v1/mcp/servers`. Document as aliases; no DB key change (`admin_settings.acp_config`, `admin_settings.mcp_servers` unchanged); secrets stay write-only masked.

Snapshot: `_build_snapshot()` keeps all current keys; Phase 2 adds `acp_alias` + `mcp_alias` reference fields (no secret raw), old readers ignore them.

## 5. Verification gates (Phase 1)

- `python3 -m py_compile admin-console/backend/*.py` pass
- `npm run build` (admin-console) pass when node available; else `tsc --noEmit` if configured
- Old routes `/providers`, `/infra`, `/runtime-config` still render; new aliases render same panels
- No systemd/docker/production change; no restart

## 6. Phase plan

- Phase 1 (this doc + process-naming doc): design-fixed, aliases designed, no deletion.
- Phase 2: frontend alias views + backend alias routes + snapshot additive fields + tests.
- Phase 3 (needs separate approval): old-label cleanup, sidebar default switch, operational rollout (backup -> change -> read-back, restarts confirmed one by one).
