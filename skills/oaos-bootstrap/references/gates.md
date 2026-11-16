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

## G5 — (Optional) Personal mail

Choice gate first: ask whether the owner wants the agent to read and draft mail. Not selected is a normal outcome — the Personal `mail` stage records `SKIP(not_selected)`, contacts no mail server, and the install completes without it.

When selected:

| Why | Link | Exact variable |
|---|---|---|
| The mailbox the agent reads and sends from (selects G5; not secret, may be given in chat) | — | `OAOS_PERSONAL_MAIL_ADDRESS` |
| An app password lets the agent log in without the account password | https://myaccount.google.com/apppasswords (Gmail; 2-Step Verification must be on) | `OAOS_PERSONAL_MAIL_APP_PASSWORD` (secret, one word, no spaces) |
| Only for non-Gmail mailboxes (Gmail defaults to `imap.gmail.com` / `smtp.gmail.com`) | provider's IMAP/SMTP settings page | `OAOS_PERSONAL_MAIL_IMAP_HOST`, `OAOS_PERSONAL_MAIL_SMTP_HOST` |

1. The user stores each value in their own terminal with `bash bootstrap/bin/oaos-secret-set <VARIABLE>`. A password passed as a process environment variable is ignored.
2. Verify immediately: `bash editions/personal/install.sh --stage mail`. It logs in over IMAP (993, TLS) and SMTP (587, STARTTLS) with the password on Python's stdin; either failure blocks the stage with the link and the variable again.
3. With Himalaya 1.x installed (Linux, macOS) the stage writes `~/.config/himalaya/config.toml` (600, account `oaos-personal`) whose `auth.cmd` is `~/.oaos/bin/oaos-mail-password` (700), a command that reads the app password from the Hermes `.env` at call time — the password is never written into the Himalaya config — and checks that Himalaya lists the inbox. An existing Himalaya config the owner wrote is preserved.
4. MANUAL cases: Himalaya not installed (link: https://pimalaya.org/himalaya/ — install a verified 1.x release, then `--stage mail --recheck`), a non-1.x version, the owner's own config, and the Windows lane (configure Himalaya by hand with a password command; never a plain-text password). After checking such a setup, record it with `bash bootstrap/verify/personal-verify.sh --confirm mail`.

## G0 — Server (Project / Company)

- Sizing target: 4 vCPU / 16 GB RAM / 200 GB disk, Ubuntu 22.04 or 24.04 LTS. Example: Hostinger KVM 4 — ≈ ₩16,095/month on the 24-month promo (verified 2026-09; renewal pricing is higher — check current). Any equivalent VPS works.
- Flow: the user purchases the VPS and logs in with an SSH key → installs Hermes **on the VPS** (official installer) → hands this repository URL to the agent running there. Everything after this runs on the VPS as one non-root administrator with non-interactive sudo; nothing runs from the user's laptop.
- The Project `prep` stage then reports capacity (WARN below the target; BLOCKED below 2 vCPU / 8 GB / 60 GB unless `OAOS_ALLOW_SMALL_HOST=1`), blocks when ports 80, 443, 3000, 5432, 6379 or 8065 are already taken by another program, reports whether SSH key login is set up and whether password login is still enabled, and adds UFW rules for SSH (22 and the port of the current SSH session), 80 and 443 **before** enabling UFW, so the current session is not cut off. `OAOS_UFW_ENABLE=0` leaves UFW inactive (verify reports FAIL). Unattended upgrades are not configured.
- Gate result: `prep` done; the capacity line is in the install log.

## G4 · G5 in Project

Not offered. Large files (G4) are deferred for every edition, and Project's mail is the G9 bot mailbox only — never a person's mailbox. A Personal G5 mailbox is not migrated to Project.

## G6 — Domain DNS (Project / Company)

- Why: Mattermost, Outline and the portal need HTTPS names. A domain is required for Project.
- Give the user a record table: `chat`, `note`, `portal` → A → the VPS public IPv4 (the agent reads it with `curl https://api.ipify.org`). The user stores the base domain with `bash bootstrap/bin/oaos-secret-set OAOS_BASE_DOMAIN` (or `OAOS_CHAT_DOMAIN`, `OAOS_NOTE_DOMAIN`, `OAOS_PORTAL_DOMAIN` for other names).
- The certificate e-mail defaults to the G9 bot address; store `OAOS_ACME_EMAIL` only to use a different one.
- `stack` installs PostgreSQL, Redis, Mattermost and Outline as host services bound to 127.0.0.1. `ingress` polls DNS until all three names resolve to the server (up to `OAOS_DNS_TIMEOUT` seconds, default 600), then configures nginx and requests certificates. On timeout it names the records still wrong; rerun `--stage ingress` after fixing them.
- Changing a domain later: store the new value, then run `OAOS_ACCEPT_DOMAIN_CHANGE=1 bash editions/project/install.sh --recheck --stage stack,ingress`. Managed vhosts are re-rendered and the certificate reinstalled.
- Gate result: all three names answer over HTTPS.

## G7 — Mattermost bot (Project)

Principle: the agent uses a **bot account**, never a person's account and never an administrator.

1. A person opens `https://chat.<domain>` and creates the first administrator account in the browser.
2. That administrator opens System Console → Integrations → Bot Accounts, creates a bot for Hermes (do not give it the System Admin role) and copies its token.
3. The user stores it: `bash bootstrap/bin/oaos-secret-set MATTERMOST_TOKEN`.
4. The user collects the Mattermost user IDs (26 characters; System Console → Users) of the members the bot may answer and stores them, comma-separated: `bash bootstrap/bin/oaos-secret-set MATTERMOST_ALLOWED_USERS`.
5. Verify immediately: `bash editions/project/install.sh --stage team`. It reads `/api/v4/users/me` with the token on curl's stdin config and blocks unless `is_bot` is true and the roles do not include `system_admin`; it checks that every allowlisted ID exists and stores `MATTERMOST_URL`. The checked `mmctl --local` release cannot create bots or tokens; the agent uses it for administrator and SiteURL checks only.

## G8 — Outline AI-agent account and API token (Project)

Principle: the AI agent writes and maintains the wiki under its **own** Outline account; documents it creates carry that account's name, and only direct human edits carry a person's name. The agent account's identity is the G9 bot mailbox.

1. A person signs up first at `https://note.<domain>`; the first account becomes the workspace admin. Outline v1.10.1 offers no e-mail sign-in before a workspace exists, so this first sign-in uses an identity provider (store its values as `OUTLINE_OIDC_CLIENT_ID`, `OUTLINE_OIDC_CLIENT_SECRET`, … — the installer writes them into `/etc/oaos/outline.env`) or, for test servers only, `POST /api/installation.create`.
2. The admin enables e-mail sign-in (Settings → Security) and invites the AI-agent account — its e-mail is the G9 bot address — as a member or editor, not admin.
3. The agent account signs in through the link delivered to the bot mailbox (G9 SMTP must work), opens Settings → API Keys and creates a token (`ol_api_…`).
4. The user stores it: `bash bootstrap/bin/oaos-secret-set OUTLINE_API_TOKEN`.
5. Verify immediately: `--stage team` calls `POST /api/auth.info` (token on curl's stdin config) and blocks unless the account e-mail equals `OAOS_MAIL_ADDRESS` and the role is not admin; it stores `OUTLINE_URL`. Once the check passes, `team` installs the `outline-wiki` skill (`skills/outline-wiki`) into `$HERMES_HOME/skills`; its stdlib helper reads `OUTLINE_URL` and `OUTLINE_API_TOKEN` from the Hermes `.env` and searches, reads, creates and updates documents as the agent account through member endpoints only. `project-verify.sh` runs its `auth.info` and a read-only search (row "Outline skill").

## G9 — Bot mailbox (Project / Company)

- Why: the agent reads and sends team mail through a mailbox of its own, so it never holds a person's account (information-security separation).
- Create a dedicated mailbox (for Gmail: a new account, 2-step verification on, then https://myaccount.google.com/apppasswords). The user stores `OAOS_MAIL_ADDRESS` and `OAOS_MAIL_PASSWORD` (app password without spaces); for providers other than Gmail also `OAOS_MAIL_IMAP_HOST` and `OAOS_MAIL_SMTP_HOST` (Gmail defaults to `imap.gmail.com` / `smtp.gmail.com`).
- Verify immediately: `--stage mail` logs in over IMAP (993) and SMTP (587, STARTTLS) without printing the password, then configures Himalaya 1.x (password read from the Hermes `.env`) and Outline SMTP.
- Confirm one real send + receive through the agent, then record it with `project-verify.sh --confirm mail-flow`.

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
