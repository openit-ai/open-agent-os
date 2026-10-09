# Gate Reference — exact scripts for each gate

Expands each gate referenced by the bootstrap procedure. Apply the gate policy from SKILL.md at all times: one gate at a time, never echo secrets, verify every value before moving on.

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

| Option | How | Notes |
|---|---|---|
| Nous Portal | `hermes setup --portal` | One OAuth covers a model + tool gateway. Fastest path. |
| Low-cost flat plan | e.g. OpenCode Go — https://opencode.ai/go | ≈ $10/month (first month ≈ $5, verified 2026-09). Sign up → create API key → paste. |
| Any OpenAI-compatible endpoint | Provider docs | Set the provider's key + base URL via `hermes config set`. |

Steps:

1. Give the user the signup link; ask them to create an API key.
2. They provide the key through the gate. Write it with `hermes config set <PROVIDER>_API_KEY <value>` (UPPER_SNAKE → `.env` under the resolved `HERMES_HOME`). Do not echo it back or put it in logs/state. Linux/macOS: owner-only mode 600; Windows: protected NTFS ACL read-back, not a `chmod 600` assumption.
3. Verify immediately: `hermes chat -q "Reply with exactly: OK"` must return OK. If not, run `hermes model` to check provider/model selection.

Common failures: `401` → key wrong or truncated (ask for a re-paste, never re-display); `model not found` → run the `hermes model` wizard.

## G2 — Telegram bot token

Open `t.me/BotFather` → send `/newbot` → choose a display name and a username ending in `bot` → copy the token (format `123456789:ABC...`). Store it with `hermes config set TELEGRAM_BOT_TOKEN <token>` in the resolved Hermes `.env`; never echo the value. Check mode/owner on Linux/macOS or protected NTFS ACL on Windows. If it leaks, revoke it in BotFather (`/revoke`) and repeat this gate.

## G3 — Telegram user ID

Open `t.me/userinfobot` → copy the numeric user ID (not the username). Store it with `hermes config set TELEGRAM_ALLOWED_USERS <numeric-id>`; never echo the value. Install and check the gateway with `hermes gateway install` and `hermes gateway status`. On Linux, read back the systemd user unit and linger; on macOS, launchd registration and status; on Windows, the ONLOGON task or Hermes Startup fallback and status. A macOS/Windows logout/login check still needs real device evidence. Ask the user to message the bot and confirm a reply.

Groups: bots only see `/commands` and replies by default. To let the bot read group messages, disable privacy mode in BotFather (`/mybots` → Bot Settings → Group Privacy → Turn off) **and re-add the bot to the group**, or promote it to group admin.

## G4 — (Optional) Large files

Open `https://my.telegram.org` to obtain `api_id` and `api_hash` for optional large-file support. Copy both values only into the agent's secret configuration; never put them in logs or the wiki.

## G5 — (Optional) Email

Enable two-factor authentication for Gmail, then create and copy an app password for optional email access. Store it in the secret configuration and do not display it again.

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

Company expansion gates G10 (admin console) and G13 (optional multi-LLM) belong to the Company bootstrap.

## Safety reminders (all gates)

- Secrets go into the resolved Hermes `.env` via `hermes config set` — owner-only mode 600 on Linux/macOS; protected NTFS ACL on Windows. Never put raw values into chat replies, logs, state files, or docs.
- Verify every pasted value immediately; a gate is not done until the check passes.
- When a gate cannot pass (e.g. user cannot create an account right now), record it in state as blocked and continue with what can proceed — do not fake success.
