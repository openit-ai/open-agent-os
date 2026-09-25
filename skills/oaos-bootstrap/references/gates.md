# Gate Reference — exact scripts for each gate

Expands each gate referenced by the bootstrap procedure. Apply the gate policy from SKILL.md at all times: one gate at a time, never echo secrets, verify every value before moving on.

## G0 — Edition choice

Present a condensed table and your recommendation:

| | Personal | Project | Company |
|---|---|---|---|
| For | 1 person | team (2–6) | company (5–50) |
| Runs on | mini PC (N100-class) | one VPS | Project + governance |
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
2. They paste the key in chat. Write it with `hermes config set <PROVIDER>_API_KEY <value>` (UPPER_SNAKE → `.env`, mode 600). Do not echo it back.
3. Verify immediately: `hermes chat -q "Reply with exactly: OK"` must return OK. If not, run `hermes model` to check provider/model selection.

Common failures: `401` → key wrong or truncated (ask for a re-paste, never re-display); `model not found` → run the `hermes model` wizard.

## G2 — Telegram bot token + user ID

1. **Token:** open `t.me/BotFather` → send `/newbot` → choose a display name → choose a username ending in `bot` → copy the token (format `123456789:ABC...`).
2. **User ID:** open `t.me/userinfobot` → it replies with a numeric ID (not the username).
3. Configure (values never echoed):
   ```bash
   hermes config set TELEGRAM_BOT_TOKEN <token>
   hermes config set TELEGRAM_ALLOWED_USERS <numeric-id>
   ```
4. Install + start the gateway and check:
   ```bash
   hermes gateway install      # or, boot-time system service: hermes gateway install --system (as root)
   hermes gateway status
   ```
5. Verify: ask the user to message the bot — a reply confirms the round-trip.
6. If the token leaks: revoke it in BotFather (`/revoke`) and redo the gate.

Groups: bots only see `/commands` and replies by default. To let the bot read group messages, disable privacy mode in BotFather (`/mybots` → Bot Settings → Group Privacy → Turn off) **and re-add the bot to the group**, or promote it to group admin.

## G3 — VPS + domain (Project / Company)

- Sizing target: 4 vCPU / 16 GB RAM / 200 GB disk. Example: Hostinger KVM 4 — ≈ ₩16,095/month on the 24-month promo (verified 2026-09; renewal pricing is higher — check current). Any equivalent VPS works.
- Steps: user purchases → provides the IP + root/SSH access → agent hardens (SSH keys, `ufw`, unattended upgrades) → domain A record → nginx + TLS (certbot).
- Gate result: IP + domain reachable over HTTPS.

## G4 — Bot mailbox (Project / Company)

- Create a dedicated mailbox for the bot (any provider; app password if 2FA).
- Configure mail access (e.g. Himalaya for IMAP/SMTP) with the app password stored in `.env`.
- Verify: send + receive one test message through the agent.

## G5 — Optional integrations (Company)

Choice gate — connect now or later; never wire one the user did not choose:

- **Slack** · **Notion** · **Google Workspace** · **Microsoft 365** — each has its own OAuth/app setup.
- Reminder for framing: these are subscriptions the company already pays for; connecting adds no new cost.

## Safety reminders (all gates)

- Secrets go into `.env` (mode 600) via `hermes config set` — never into chat replies, logs, state files, or docs.
- Verify every pasted value immediately; a gate is not done until the check passes.
- When a gate cannot pass (e.g. user cannot create an account right now), record it in state as blocked and continue with what can proceed — do not fake success.
