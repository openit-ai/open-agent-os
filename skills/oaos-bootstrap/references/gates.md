# Gate Reference — exact scripts for each gate

Expands each gate referenced by the bootstrap procedure. Apply the gate policy from SKILL.md at all times: one gate at a time, never echo secrets, verify every value before moving on.

Every secret below is entered by the user in their own terminal with `bash bootstrap/bin/oaos-secret-set KEY` (hidden prompt, or stdin from a script). It writes `KEY=value` into the resolved Hermes `.env` atomically and reads back owner-only protection (mode 600 + owner on Linux/macOS, protected NTFS ACL on Windows). The value never appears in chat, argv, logs or state. Do not use `hermes config set KEY <secret>`.

## Edition choice (before the gates)

Present a condensed table and your recommendation:

| | Personal | Project | Company |
|---|---|---|---|
| For | 1 person | team (2–6) | company (5–50) |
| Runs on | Ubuntu mini PC, Apple Silicon Mac, or Windows 11 PC (Git Bash) | Ubuntu LTS VPS | Ubuntu LTS Project + governance |
| Chat | Telegram | Mattermost + Telegram | + Slack (optional) |

Recommendation logic: one person on a mini PC → **Personal**; team chat and shared docs → **Project**; policy/audit/permissions needed → **Company**. No external links needed.

## G1 — LLM plan

Goal: a model provider the agent can call.

| Option | How | Exact variable | Notes |
|---|---|---|---|
| Nous Portal | `hermes setup --portal` | none (OAuth login) | One OAuth covers a model + tool gateway. Fastest path. |
| OpenCode Go (low-cost flat plan) | https://opencode.ai/go → create API key | `OPENCODE_GO_API_KEY` | ≈ $10/month (first month ≈ $5, verified 2026-09). |
| OpenCode Zen | https://opencode.ai → API key | `OPENCODE_ZEN_API_KEY` | Pay-as-you-go. |
| OpenRouter | https://openrouter.ai/keys | `OPENROUTER_API_KEY` | Many models behind one key. |
| Vercel AI Gateway | Vercel dashboard → AI Gateway → API keys | `AI_GATEWAY_API_KEY` | Not `VERCEL_AI_GATEWAY_API_KEY`. |
| OpenAI / Anthropic | Provider console | `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | |
| Other OpenAI-compatible endpoint | Provider docs | as `hermes model` names it | Enter the base URL in `hermes model`; the key through the helper. |

Names follow Hermes v0.23 (`hermes_cli/auth.py` provider table); when `hermes model` names a different variable, use that name.

Steps:

1. Give the user the signup link; ask them to create an API key (or run `hermes setup --portal` for Nous Portal OAuth).
2. The user runs `bash bootstrap/bin/oaos-secret-set <EXACT_VARIABLE>` in their own terminal, then `hermes model` to pick provider and model.
3. Verify immediately: `bash editions/personal/install.sh --stage llm`. It passes only when a bounded `hermes chat -q "Reply with exactly: OK"` answers within 30 s; a stored key with no working model blocks (exit 3).

Common failures: `401` → key wrong or truncated (ask for a re-paste, never re-display); `model not found` → run the `hermes model` wizard.

## G2 — Telegram bot token

Open `t.me/BotFather` → send `/newbot` → choose a display name and a username ending in `bot` → copy the token (format `123456789:ABC...`). The user stores it with `bash bootstrap/bin/oaos-secret-set TELEGRAM_BOT_TOKEN`. Verify: `--stage telegram` calls Telegram `getMe` with the token on curl's stdin config (never argv) and blocks with the HTTP code on failure. If it leaks, revoke it in BotFather (`/revoke`) and repeat this gate.

## G3 — Telegram user ID

Open `t.me/userinfobot` → copy the numeric user ID (not the username). The user stores it with `bash bootstrap/bin/oaos-secret-set TELEGRAM_ALLOWED_USERS`; format is numbers only, comma-separated for several people (`12345,67890`). The telegram stage rejects anything else. The `gateway` stage then installs and checks the gateway (`hermes gateway install`, `hermes gateway status`). On Linux, it reads back the systemd user unit and linger; on macOS, launchd registration and status; on Windows, the ONLOGON task or Hermes Startup fallback and status. Reboot or logout/login survival needs real device evidence on every lane. Ask the user to message the bot and confirm a reply.

Groups: bots only see `/commands` and replies by default. To let the bot read group messages, disable privacy mode in BotFather (`/mybots` → Bot Settings → Group Privacy → Turn off) **and re-add the bot to the group**, or promote it to group admin.

## G4 — (Deferred) Large files

Not implemented. The Telegram local Bot API (needs `api_id`/`api_hash` from `https://my.telegram.org` and a separate server) is deferred; do not offer it as part of the Personal install. If the user asks, explain that files over 20 MB are not supported yet.

## G5 — (Optional, manual) Email

The Personal installer has no mail stage yet. If the user wants mail now: enable two-factor authentication for the mailbox (Gmail: https://myaccount.google.com/apppasswords), create an app password, and store it with `bash bootstrap/bin/oaos-secret-set <VARIABLE>` under a variable name you choose (for example `OAOS_MAIL_PASSWORD`), and configure the mail tool (for example Himalaya) to read it from the environment or a password command — never as plain text in the tool's config file. Confirm one IMAP login without printing the password. Record it as a manual, optional step.

## G0 · G6 — VPS + domain (Project / Company)

- Sizing target: 4 vCPU / 16 GB RAM / 200 GB disk. Example: Hostinger KVM 4 — ≈ ₩16,095/month on the 24-month promo (verified 2026-09; renewal pricing is higher — check current). Any equivalent VPS works.
- Steps: user purchases → provides the IP + root/SSH access → agent installs packages and adds OpenSSH/80/443 to existing `ufw` rules → three domain A records (`chat`, `note`, `portal`) → nginx + TLS (certbot). Confirm SSH key login, whether `ufw` is active, and unattended upgrades separately; the installer does not enable or configure those policies.
- Project installs the bot mailbox (G9), then handles the Mattermost and Outline admin gates (G7/G8), then starts the gateway for the first time.
- Project runs PostgreSQL, Redis, Mattermost and Outline as host services. Confirm the Outline listener is only on `127.0.0.1:3000` before completing G6.
- Gate result: IP + domain reachable over HTTPS.

## G7 — Mattermost admin (Project)

Open the initial Mattermost URL provided by the agent and create the first administrator account in the browser. Create the Hermes bot and token in System Console, then let the agent store and verify the token. The checked `mmctl --local` release cannot create bots or tokens; the agent uses it for administrator and SiteURL checks.

## G8 — Outline API token (Project)

Sign in as the Outline administrator, issue an API token, and paste it to the agent. The agent stores it as a secret and verifies it without echoing it.

## G9 — Bot mailbox (Project / Company)

- Create a dedicated mailbox for the bot (any provider; app password if 2FA). Keep it separate from personal mail so the bot cannot access a person's inbox.
- Configure mail access (e.g. Himalaya for IMAP/SMTP) with the app password stored in `.env`.
- Verify: send + receive one test message through the agent.

## G11 · G12 — Optional integrations (Company)

Choice gate — connect now or later; never wire one the user did not choose:

- **G11, productivity suite:** Google Workspace or Microsoft 365 — connect the suite the organization already uses.
- **G12, collaboration tools:** Slack or Notion — connect only tools the organization already uses.
- Reminder for framing: these are subscriptions the company already pays for; connecting adds no new cost.
- Adapter stages: `install.sh --stage c11..c15` configure each chosen service (`c11` Slack, `c12` Notion, `c13` Google Workspace, `c14` Microsoft 365, `c15` closes the G11/G12 gates). A service not chosen is recorded as `SKIP(reason=not_selected)` with no credential or service call. Google Workspace and Microsoft 365 are mutually exclusive.

Company expansion gates G10 (admin console) and G13 (optional multi-LLM) belong to the Company bootstrap.

## Safety reminders (all gates)

- Secrets go into the resolved Hermes `.env` via `bash bootstrap/bin/oaos-secret-set KEY` — owner-only mode 600 on Linux/macOS; protected NTFS ACL on Windows. Never put raw values into chat replies, argv, logs, state files, or docs.
- Verify every pasted value immediately; a gate is not done until the check passes.
- When a gate cannot pass (e.g. user cannot create an account right now), record it in state as blocked and continue with what can proceed — do not fake success.
