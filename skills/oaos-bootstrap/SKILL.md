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
2. Installer stage state: `~/.oaos-install/state.json` — on resume, read it and continue from the first incomplete stage. Re-running completed stages must be safe.
3. Log: `~/.oaos/logs/install-YYYYMMDD.log` — append command summaries and results. **Never write secrets to the log or state file.**

## Quick Reference

| Phase | What happens | Gate |
|---|---|---|
| 0 — Recon | OS, resources, existing install; recommend an edition | — |
| 1 — Edition | User picks Personal / Project / Company | Choice |
| 2 — Prep | Packages, timezone, swap, directories | — |
| 3 — Install | Personal: Hermes → LLM → Telegram → gateway → wiki → harness → cron; Project: Hermes → LLM → Telegram → wiki → harness → cron → stack → ingress → mail → team → gateway | G0·G1–G9 (by edition) |
| 4 — Verify | The full verify checklist — every check must run | — |
| 5 — Report | What was built, addresses, first things to try | — |

## Procedure

### Phase 0 — Recon (read-only)

Run and record:

```bash
uname -a; cat /etc/os-release; nproc; free -h; df -h /; swapon --show
ls ~/.hermes 2>/dev/null; hermes --version 2>/dev/null; hermes doctor 2>/dev/null | head -20
```

Recommend an edition from the README table (default: Personal). Keep the recon summary in the session; the selected installer creates stage state when it runs.

Completion: the user has seen a 3–5 line summary; a recommended edition is on the table.

### Phase 1 — Edition choice

Present a condensed editions table and your recommendation; wait for the choice. Record the choice in the session; the selected installer records its edition in stage state.

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

Register the daily backup of `~/.hermes` + the wiki with the Hermes cron system (see Hermes docs → Scheduled automations). Project uses this backup job only; Personal retains its existing backup and watchdog jobs. Confirm the applicable jobs appear in the cron list.

#### 3e. Harness seeding (all editions)

Run the seeding conversation from this repository's `harness/seeding.md`: draft `SOUL.md` and `USER.md` with the user; pre-fill `MEMORY.md` with environment facts (the agent maintains it automatically afterward — the user reviews, not authors). Write to `~/.hermes/SOUL.md` and `~/.hermes/memories/`. Use the templates in `harness/templates/` as starting points; mention `AGENTS.md` only when the user has project workspaces.

#### 3f. Project edition (P1)

On the Project VPS, follow `editions/project/README.md` and run `bash editions/project/install.sh --dry-run` first. The 13 stages are `prep hermes llm telegram wiki harness cron stack ingress mail team gateway verify`. Run `bash editions/project/install.sh` to continue; `--stage stack,ingress,mail`, `--status`, `--skip-verify`, and `--timezone Area/City` are available for recovery. Stage state is `~/.oaos-install/state.json` with `edition=project`. A blocked gate exits 3; a failed command exits 1. Keep each stage's evidence and resume only after its prerequisite is satisfied.

- **G0**: prepare an Ubuntu LTS VPS (4 vCPU / 16 GB / 200 GB recommended), SSH access and root rights. `prep` installs host packages, Docker Engine and Compose; it adds OpenSSH, 80 and 443 to existing UFW rules without resetting them.
- **G1–G3**: provide the LLM key, Telegram bot token and allowed user IDs as in Personal. The Project wiki, harness and daily backup mirror the Personal setup. Project starts the gateway after team configuration so both Telegram and Mattermost are present at first start.
- **G6**: set `OAOS_BASE_DOMAIN=example.com` (or `OAOS_CHAT_DOMAIN`, `OAOS_NOTE_DOMAIN`, `OAOS_PORTAL_DOMAIN` separately), and `OAOS_ACME_EMAIL`. Add A records for `chat`, `note`, `portal` pointing to the VPS public IPv4. `stack` starts Mattermost, Outline, PostgreSQL and Redis; `ingress` checks DNS, configures nginx and requests TLS certificates. The stack keeps secrets in `~/oaos/stack/.env` (600); never paste those values into logs or chat.
- **G9**: supply `OAOS_MAIL_ADDRESS`, `OAOS_MAIL_PASSWORD`, `OAOS_MAIL_IMAP_HOST`, `OAOS_MAIL_SMTP_HOST` from a dedicated bot mailbox. `mail` configures Himalaya 1.x when present and restarts Outline with SMTP. Confirm IMAP login and one real send/receive flow.
- **G7**: create the first Mattermost administrator at `https://chat.<domain>` in the browser. `team` checks the administrator, uses `mmctl` to create a Hermes bot and token when possible, writes `MATTERMOST_URL` and `MATTERMOST_TOKEN` through `hermes config set`, then creates `~/oaos/team-onboarding.md`. If `mmctl` cannot create a token, use the Mattermost System Console and store `MATTERMOST_TOKEN` in the stack `.env` before rerunning. Set `MATTERMOST_ALLOWED_USERS` for team members and test an allowed and a denied account.
- **G8**: sign in as an Outline administrator at `https://note.<domain>`, issue an API token, and verify it against the Outline API without logging the value. The token stays in Hermes `.env` for the agent's Outline API access. Outline email sign-in requires the G9 SMTP values configured earlier.

이미 게이트웨이가 실행 중인 상태에서 team 구성을 다시 적용한 경우에만, 관리자(사용자)가 별도 셸에서 게이트웨이 서비스를 재시작해야 반영됩니다.

Run `bash bootstrap/verify/project-verify.sh` (or `--json`) after the gates. DNS, certificates, nginx, HTTPS, Compose health, Mattermost, Outline, IMAP and Personal checks are reported separately as PASS/FAIL/MANUAL/SKIP. A real VPS end-to-end run is still required to claim P1 completion. To move a Personal wiki and harness, run `bash bootstrap/migrate/personal-to-project.sh --source user@host --dry-run`, then repeat without `--dry-run`; use `--include-config` only if its `config.yaml` should be copied. `.env` is excluded and credentials must be re-entered.

#### Company edition (P2)

Company extends Project with the governance layer (admin console, policy, audit, vault, permission-aware knowledge index), optional connectors and multi-LLM routing. Its installer belongs to P2; report the current status instead of claiming it is available.

### Phase 4 — Verify

Run the full checklist in `references/verify-checklist.md`. Every check must actually run; report only what actually passed.

### Phase 5 — Report & handoff

Report format (keep it tight):

- Edition built · bot handle / addresses · wiki path
- What to try first (link `docs/cookbook.md`)
- Backup location (`~/.oaos/backups`; `hermes backup` for full state)
- MANUAL 실증 필요 목록과 확인 담당자 (Project 검증의 MANUAL 수는 설치 로그·state detail에 기록)
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

- Run every applicable item in `references/verify-checklist.md`; record PASS, FAIL, MANUAL and SKIP with command evidence. Do not claim completion while a required check is unresolved.
- Record the installer stage state; deliver the report and confirm one real bot interaction.
