# Adapters — External Integrations (Section 32 / P1-1)

All adapters live under `adapters/<name>/adapter.py` and expose a uniform MCP surface:
`list_tools() -> list[str]`, `list_resources() -> list[str]`, `call_tool(name, args, agent_context)`,
plus provider-specific OAuth / webhook / ACL helpers.

## Google (`adapters/google`)

- **역할**: Personal tools — Gmail / Calendar / Drive / Tasks (Sections 9-10). Owner isolation, delegation_id Vault binding.
- **Env**: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, optional `GOOGLE_SCOPES`.
- **OAuth 2.0 authorization_code**:
  1. `authorize_url(delegation_id, user_id, scopes?) -> (url, state)` — CSRF state persisted in-memory (prod: Redis).
  2. User consents at Google → redirect with `code`.
  3. `exchange_code(code, state) -> {delegation_id, scope, secret_ref}` — validates state, POSTs `https://oauth2.googleapis.com/token`, stores `access_token::refresh_token` bundle encrypted in Vault, creates `CredentialBinding` via `delegation_service` if provided.
  4. `refresh(refresh_token) -> TokenSet` — rotates access token.
  5. `revoke(token)` / `revoke_delegation(delegation_id, token?)` — revokes at `https://oauth2.googleapis.com/revoke` and cascades via DelegationService.
  6. `get_access_token(secret_ref, agent_id) -> str` — Vault decrypt (owner check).
- **MCP**: `list_tools` = 12 tools (`gmail_search/read/send`, `calendar_*`, `drive_*`, `tasks_*`), `list_resources` = `gmail/user/*` etc, `call_tool` does owner check (`resource owner == agent_context.user_id`), resolves token from Vault via delegation binding, builds httpx Bearer request to googleapis (skeleton returns `planned_request` when no token).
- **Scopes**: least-privilege per tool (e.g. `gmail.readonly` for search/read, `gmail.send` only for send, `calendar.readonly` vs `calendar`, `tasks.readonly` vs `tasks`).
- **테스트**: `pytest -q` 통과 유지; OAuth는 `httpx.AsyncClient` mock으로 단위 테스트 가능. Skeleton 모드(토큰 없이 `call_tool` 호출)면 `requires_token` + `planned_request` 반환으로 오프라인 테스트 가능.

## Outline (`adapters/outline`)

- **역할**: Shared knowledge — collections / documents / search (§28 retrieval 전 ACL).
- **Env**: `OUTLINE_API_URL` (default `https://app.getoutline.com`), `OUTLINE_API_KEY`.
- **ACL §28**: `check_acl(agent_context, resource)` — gateway pre-filter before retrieval (tenant isolation, private collection hint). `filter_collections()` — post-filter on returned collections. `can_write()` — stricter for CREATE/MODIFY. 최종 문서 ACL은 Outline API가 강제, gateway는 사전 차단 + defense-in-depth.
- **MCP**: 8 tools (`outline_search/read/create/modify`, `outline_collections_*`, `outline_documents_*`), resources `outline/*`, `call_tool` does ACL pre-check then `POST /api/documents.search|info|create|update|collections.list|info|documents.list` via httpx (skeleton when no API key).
- **테스트**: ACL 단위 테스트는 `agent_context` dict로 tenant/group 시나리오 커버; API는 no-key skeleton으로 오프라인 검증.

## Mattermost (`adapters/mattermost`)

- **역할**: Human workspace — incoming webhook HMAC + outgoing Bot API + identity mapping §14.
- **Env**: `MATTERMOST_URL`, `MATTERMOST_BOT_TOKEN`, `MATTERMOST_WEBHOOK_SECRET`.
- **Identity §14**: `map_mattermost_user(mm_user_id, username) -> employee:principal`, `register_identity(mm_user_id, employee:principal)`, `reverse_map(employee) -> mm_user_id`. Canonical `employee:<suffix>` / `agent:assistant:<suffix>`.
- **Webhook**: `verify_signature(body, signature) -> bool` (HMAC-SHA256 over body, dev: no secret → accept), `parse_incoming(payload) -> {mattermost_user_id, employee_principal, text, channel_id, team_id}`.
- **Outgoing**: `send_message(channel_id, text, props?, root_id?) -> Mattermost post` via `POST /api/v4/posts` with Bearer token, plus `get_user`, `list_channels`. Skeleton when URL/token missing.
- **MCP**: 5 tools (`mattermost_send_message`, `create_post`, `list_channels`, `get_user`, `search_posts`), resources `mattermost/channel/*` etc.

## Slack (`adapters/slack`)

- **역할**: Slack workspace — specular to Mattermost (Bot API + Signing Secret + OAuth v2).
- **Env**: `SLACK_BOT_TOKEN` (xoxb-), `SLACK_SIGNING_SECRET`, `SLACK_CLIENT_ID`/`SLACK_CLIENT_SECRET`, `SLACK_REDIRECT_URI`.
- **OAuth**: `authorize_url(state, scopes?)`, `exchange_code(code)` → `oauth.v2.access`.
- **Webhook**: `verify_signature(body, timestamp, signature)` — `v0:{ts}:{body}` HMAC with 5-min replay window; `parse_incoming`.
- **Identity §14**: `map_slack_user` / `register_identity` (same `employee:` derivation).
- **MCP**: 7 tools (`slack_send_message`, `post_message`, `list_channels`, `get_user`, `search_messages`, `create_channel`, `add_reaction`), `SCOPES` per tool (chat:write, channels:read 등).

## Microsoft (`adapters/microsoft`)

- **역할**: Microsoft 365 Graph — Mail / Calendar / Drive / Tasks, Azure AD OAuth (google adapter와 동일 패턴).
- **Env**: `MS_CLIENT_ID` / `MICROSOFT_CLIENT_ID`, `MS_CLIENT_SECRET`, `MS_TENANT_ID` (`common` default), `MS_REDIRECT_URI`.
- **OAuth**: `authorize_url(delegation_id, user_id) -> (url, state)` → `login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize|token`, `exchange_code`, `refresh` (Graph scopes `Mail.Read`, `Calendars.Read` 등).
- **MCP**: 10 tools (`ms_mail_*`, `ms_calendar_*`, `ms_drive_*`, `ms_tasks_*`), resources `microsoft/mail/*` 등, `call_tool`에 owner isolation + Vault skeleton.

## Notion (`adapters/notion`)

- **역할**: Shared knowledge — databases / pages / search (Outline과 동일 ACL 패턴 §28).
- **Env**: `NOTION_API_KEY` (`secret_...`), `NOTION_API_URL` (default `https://api.notion.com`).
- **ACL §28**: `check_acl` 사전 차단, final ACL은 Notion 권한.
- **MCP**: 7 tools (`notion_search`, `read_page`, `read_database`, `create_page`, `update_page`, `query_database`, `list_databases`), resources `notion/*`, Notion API header `Notion-Version: 2022-06-28`, httpx skeleton when no key.

## IAM (`adapters/iam`)

- **역할**: Directory sync — Google Workspace / Azure AD / Okta user·group 동기화, principal mapping §14, tenant isolation.
- **Env**: `IAM_PROVIDER` (google|azure|okta), `IAM_DOMAIN`, `IAM_API_KEY`.
- **Mapping §14**: `to_employee_principal(email) -> employee:`, `to_agent_principal(employee) -> agent:assistant:`, `resolve_principal(email_or_id)`.
- **Directory**: `get_user`, `list_users`, `get_group`, `list_groups` (httpx when key present, else local cache + skeleton), `sync_users(users)`, `sync_groups(groups)` for admin bulk sync.
- **MCP**: 6 tools (`iam_get_user`, `list_users`, `get_group`, `list_groups`, `sync_users`, `resolve_principal`), resources `iam/user/*`, `iam/group/*`.

## Hermes (`adapters/hermes`)

- **역할**: Internal Agent Interface bridge (§17) — session lifecycle, prompt forwarding, stream, trace propagation. MCP-facing view of Hermes; real transport is `ACPAdapter`/`InternalAgentInterface`.
- **Env**: `HERMES_BASE_URL`, `HERMES_API_KEY`, `HERMES_MOCK=1` for skeleton mode.
- **Interface §17**: `create_session(agent_context)`, `send_prompt(session_id, prompt, agent_context, request_id?)`, `get_session`, `cancel_session`, `stream_events` (async generator, SSE skeleton). All propagate `X-Trace-Id` / `X-Agent-Context`.
- **MCP**: 6 tools (`hermes_create_session`, `send_prompt`, `get_session`, `stream_events`, `cancel_session`, `list_sessions`), resources `hermes/session/*`.

## 테스트 방법

```bash
# 전체 108 테스트 유지 확인 (P1-1 요구)
pytest -q

# 어댑터 오프라인 스켈레톤 검증 (네트워크 없이)
python -c "from adapters.google.adapter import GoogleAdapter; a=GoogleAdapter(); print(a.describe())"
python -c "from adapters.outline.adapter import OutlineAdapter; a=OutlineAdapter(api_key=''); import asyncio; print(asyncio.run(a.list_tools()))"
python -c "from adapters.mattermost.adapter import MattermostAdapter; m=MattermostAdapter(); print(m.verify_signature(b'hello', None))"

# 실제 연동 (env 설정 후)
# Google: GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI 설정 → authorize_url → exchange_code
# Outline: OUTLINE_API_URL + OUTLINE_API_KEY 설정 → call_tool('outline_search', {query:...}, context)
# Mattermost: MATTERMOST_URL + BOT_TOKEN + WEBHOOK_SECRET 설정 → send_message / verify_signature
# Slack/Microsoft/Notion/IAM/Hermes: 각 *_API_KEY / *_BOT_TOKEN 설정 후 동일 패턴
```

## 공통 규약

- 모든 adapter는 `async list_tools() -> list[str]`, `async list_resources() -> list[str]`, `async call_tool(tool, args, agent_context) -> dict` 를 구현 (기존 stub 시그니처 호환).
- `describe()` / `describe_tools()` 는 MCP discovery 상세 정보 제공.
- Vault/Delegation 연동이 필요한 adapter (google, microsoft)는 생성자에서 `vault` + `delegation_service`를 주입받으면 자동 바인딩 (없으면 skeleton 동작).
- 네트워크 의존 호출은 모두 httpx optional import + skeleton fallback으로 오프라인에서도 import/테스트가 깨지지 않음.
