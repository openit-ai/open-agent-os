---
name: oaos-bootstrap
description: "Bootstrap an Open Agent OS edition: install, gates, verify."
version: 0.1.1
author: OpenIT (openit-ai), Hermes Agent
license: Apache-2.0
platforms: [linux]
metadata:
  hermes:
    tags: [oaos, bootstrap, installation, onboarding]
    related_skills: [oaos-ops]
---

# OAOS Bootstrap Skill

Orchestrates the "one URL" setup of an Open Agent OS edition on a Linux host: recon → edition choice → install → gates → verify → report. The human handles only the gates; you (the agent) handle everything else — reading, file edits, commands, and checks.

## When to Use

- The user handed you this repository's URL and asked to set it up ("openit-ai/open-agent-os 설치해줘", "set up open-agent-os", "bootstrap OAOS").
- The user wants an OAOS edition installed or rebuilt on this machine.
- Don't use for: day-2 operations after setup (use `oaos-ops`); developing the platform code itself.

## Prerequisites

- Linux host (Ubuntu LTS recommended), a user account with admin rights (root access for the host-prep steps), internet access.
- Hermes Agent installed and working (`hermes --version`; `hermes doctor`).
- The user is present — this skill runs in their chat and needs them at the gates.
- Access to this repository's files: a local clone, or fetch raw files (`raw.githubusercontent.com/openit-ai/open-agent-os/main/...`) as needed.

## How to Run

1. Follow the Procedure top to bottom; load `references/gates.md` before Phase 3 and `references/verify-checklist.md` before Phase 5.
2. Personal installer stage state: `~/.oaos-install/state.json` — on resume, read it and continue from the first incomplete stage. Re-running completed stages must be safe.
3. Log: `~/.oaos/logs/install-YYYYMMDD.log` — append command summaries and results. **Never write secrets to the log or state file.**

## Quick Reference

| Phase | What happens | Gate |
|---|---|---|
| 0 — Recon | OS, resources, existing install; recommend an edition | — |
| 1 — Edition | User picks Personal / Project / Company | Choice |
| 2 — Prep | Packages, timezone, swap, directories | — |
| 3 — Install | Hermes check/install → LLM plan → chat platform → gateway → wiki → cron → harness seed | G1–G5 |
| 4 — Verify | The full verify checklist — every check must run | — |
| 5 — Report | What was built, addresses, first things to try | — |

## Procedure

### Phase 0 — Recon (read-only)

Run and record:

```bash
uname -a; cat /etc/os-release; nproc; free -h; df -h /; swapon --show
ls ~/.hermes 2>/dev/null; hermes --version 2>/dev/null; hermes doctor 2>/dev/null | head -20
```

Recommend an edition from the README table (default: Personal). Keep the recon summary in the session; the Personal installer creates stage state when it runs.

Completion: the user has seen a 3–5 line summary; a recommended edition is on the table.

### Phase 1 — Edition choice

Present a condensed editions table and your recommendation; wait for the choice. Record the choice in the session; the Personal installer records its edition in stage state.

Completion: edition fixed; the gate list for that edition is prepared (see `references/gates.md`).

### Phase 2 — Environment prep

```bash
# run as root:
apt-get update && apt-get install -y git curl xz-utils ca-certificates
timedatectl set-timezone <Region/City>          # optional, recommended
mkdir -p ~/.oaos/logs ~/.oaos/backups
```

Swap for small-RAM hosts — if `swapon --show` is empty and RAM < 16 GB:

```bash
# run as root:
fallocate -l 8G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' | tee -a /etc/fstab
```

Completion: packages installed; swap active (or a recorded reason why not); directories exist.

### Phase 3 — Install

For Personal, the agent can run the repository's `editions/personal/install.sh` to perform these steps idempotently; stage state is `~/.oaos-install/state.json`.

#### 3a. Hermes Agent (all editions)

- If not installed: download the official installer, inspect it, then run it:

  ```bash
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o hermes-install.sh
  # review the downloaded file, then:
  bash hermes-install.sh
  ```

  Then open a new shell session so the updated PATH applies.
- Verify: `hermes --version` and `hermes doctor` run.
- **Gate G1 — LLM plan.** Ask the user to pick their plan; present 2–3 options (a low-cost flat plan such as OpenCode Go is the recommended default for Personal; Nous Portal via `hermes setup --portal` is the one-command path). Walk them through the signup link; have them paste the key. Set it with `hermes config set <PROVIDER>_API_KEY <value>` (UPPER_SNAKE names are written to `.env`, mode 600) or use the `hermes model` wizard. Verify immediately with a one-shot call (`hermes chat -q "Reply with exactly: OK"`).

#### 3b. Chat platform (Personal default: Telegram)

- **Gates G2–G3 — bot token + user ID.** Guide: open `t.me/BotFather` → `/newbot` → copy the token. Then `t.me/userinfobot` → copy the numeric user ID.
- Configure:
  ```bash
  hermes config set TELEGRAM_BOT_TOKEN <token>
  hermes config set TELEGRAM_ALLOWED_USERS <numeric-id>
  ```
- Install the gateway service and start it:
  ```bash
  hermes gateway install          # user service; boot start: loginctl enable-linger $USER (as root)
  # or boot-time system service (Linux): hermes gateway install --system (as root)
  hermes gateway status
  ```
- Verify: the user sends a message to the bot and gets a reply.

#### 3c. Knowledge wiki (all editions)

```bash
mkdir -p ~/data/wiki && cd ~/data/wiki && git init -b main
# create index.md (seed: title, purpose, top-level sections), then:
git add index.md && git commit -m "wiki: seed index"
```

#### 3d. Scheduled jobs (all editions)

Register starter jobs with the Hermes cron system (see Hermes docs → Scheduled automations): (1) daily backup of `~/.hermes` + the wiki; (2) gateway watchdog — check and restart if down. Confirm both appear in the cron list.

#### 3e. Harness seeding (all editions)

Run the seeding conversation from this repository's `harness/seeding.md`: draft `SOUL.md` and `USER.md` with the user; pre-fill `MEMORY.md` with environment facts (the agent maintains it automatically afterward — the user reviews, not authors). Write to `~/.hermes/SOUL.md` and `~/.hermes/memories/`. Use the templates in `harness/templates/` as starting points; mention `AGENTS.md` only when the user has project workspaces.

#### 3f. Project / Company editions (P1 / P2)

Project and Company extend Personal. Their runbooks land with those phases — **do not invent commands for them.** Outline to present to the user:

- **Project** — a VPS (4 vCPU / 16 GB / 200 GB), domain + nginx + TLS, Mattermost + Outline deployment, a dedicated bot mailbox, team accounts with per-member allowlists.
- **Company** — Project + the governance layer (admin console, policy, audit, vault, permission-aware knowledge index) from the existing platform, optional Slack / Notion / Google Workspace / Microsoft 365 connectors, multi-LLM routing.

If the user picked Project or Company today, complete Personal first, then hand over the phase outline and current status honestly.

### Phase 4 — Verify

Run the full checklist in `references/verify-checklist.md`. Every check must actually run; report only what actually passed.

### Phase 5 — Report & handoff

Report format (keep it tight):

- Edition built · bot handle / addresses · wiki path
- What to try first (link `docs/cookbook.md`)
- Backup location (`~/.oaos/backups`; `hermes backup` for full state)
- Known gaps / next phase

Report the installer stage state, then point the user to `oaos-ops` for day-2.

## Gate Policy (applies to every gate)

- One gate at a time. One-line explanation of why. Wait for the result before continuing.
- **Never invent credentials.** Never echo secrets into chat, logs, or files — redact in summaries.
- Link gates: exact URL, what to click, what to copy back (if anything).
- Choice gates: 2–3 options with a recommendation.
- After each gate: verify the value works (API 200, bot replies, login succeeds) and write state.

## Pitfalls

- `hermes config set` routes `UPPER_SNAKE` names to `.env` and dotted keys to `config.yaml` — use the right form.
- Telegram is default-deny: the user must be in `TELEGRAM_ALLOWED_USERS` or paired (`hermes pairing approve telegram <code>`), or the bot will silently ignore them.
- Check swap **before** heavy installs on small hosts (OOM risk).
- Do not reboot without explicit approval. When approval is unavailable, verify boot survival via enabled services + lingering instead.
- If `hermes skills install` is unavailable in this build, read this SKILL.md from the repository and follow it manually.
- Large files (>20 MB uploads) need the Telegram local Bot API — optional, note only if asked.

## Verification

- Every item in `references/verify-checklist.md` passes, with command output as evidence.
- State file at `phase: 5`; report delivered; the user confirms one real interaction (e.g. a bot message gets a reply).
