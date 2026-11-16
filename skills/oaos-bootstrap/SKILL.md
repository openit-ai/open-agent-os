---
name: oaos-bootstrap
description: "Bootstrap an Open Agent OS edition: install, gates, verify."
version: 0.2.0
author: OpenIT (openit-ai), Hermes Agent
license: Apache-2.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [oaos, bootstrap, installation, onboarding]
    related_skills: [oaos-ops]
---

# OAOS Bootstrap Skill

Orchestrates the "one URL" setup of an Open Agent OS edition: recon → edition choice → install → gates → verify → report. Personal has Ubuntu (Linux), Apple Silicon macOS, and Windows 11 Git Bash lanes; Project and Company remain Ubuntu LTS server paths. The human handles the gates; you handle reading, file edits, commands, and checks.

## When to Use

- The user handed you this repository's URL and asked to set it up ("openit-ai/open-agent-os 설치해줘", "set up open-agent-os", "bootstrap OAOS").
- The user wants an OAOS edition installed or rebuilt on this machine.
- Don't use for: day-2 operations after setup (use `oaos-ops`); developing the platform code itself.

## Prerequisites

- Personal: Ubuntu LTS, Apple Silicon macOS, or Windows 11 with Git Bash; an account permitted to install Hermes and inspect OS settings; internet access. Project / Company: Ubuntu LTS server with administrator rights.
- Hermes Agent installed and working (`hermes --version`; health is judged by the verifier, which reads `hermes doctor` without printing it). On Personal the installer can install Hermes for you.
- A clone of the official repository (`git clone https://github.com/openit-ai/open-agent-os.git`). Installers stop unless `origin` is `github.com/openit-ai/open-agent-os`; a fork the user has reviewed needs `OAOS_ALLOW_FORK=1`.
- The user is present — this skill runs in their chat and needs them at the gates.
- Install this skill from its raw URL: `hermes skills install https://raw.githubusercontent.com/openit-ai/open-agent-os/main/skills/oaos-bootstrap/SKILL.md`.

## How to Run

1. Follow the Procedure top to bottom; load `references/gates.md` before Phase 3 and `references/verify-checklist.md` before Phase 4.
2. Installer stage state: `~/.oaos-install/state.json`, one namespace per edition (`editions.<edition>.stages`). On resume run the installer's `--status` and continue from the first incomplete stage. Re-running completed stages is safe; `verify` always reruns.
3. Log: `~/.oaos/logs/install-YYYYMMDD.log` — the installer writes redacted lines. **Never write secrets to the log or state file.**
4. Secrets: the user types them in their own terminal with `bash bootstrap/bin/oaos-secret-set KEY` (hidden prompt; stores `KEY=value` in the Hermes `.env`, owner-only). Never ask for a secret in chat and never pass one as a command argument (`hermes config set KEY <secret>` puts it in argv and shell history).

## Quick Reference

| Phase | What happens | Gate |
|---|---|---|
| 0 — Recon | OS, resources, existing install; recommend an edition | — |
| 1 — Edition | User picks Personal / Project / Company | Choice |
| 2 — Prep | Packages, timezone, swap, directories | — |
| 3 — Install | Personal stages: prep → hermes → llm → telegram → gateway → wiki → harness → cron → verify; Project: prep → hermes → llm → telegram → wiki → harness → cron → stack → ingress → mail → team → gateway → verify | Hermes-install approval · Personal G1–G3 (+G5 manual); Project G0–G3, G6–G9 |
| 4 — Verify | The edition verifier — every check must run; MANUAL items need human evidence | — |
| 5 — Report | What was built, addresses, first things to try | — |

## Procedure

### Phase 0 — Recon (read-only)

First identify the OS and architecture with `uname -s`, `uname -m`, and `uname -a`. Windows commands below run **inside Git Bash**; PowerShell is used only for Windows system queries and the official Hermes installer. Run the matching read-only commands and record the results:

```bash
# Ubuntu (Personal, Project, Company)
cat /etc/os-release; nproc; free -h; df -h /; swapon --show
# macOS Personal: confirm uname -m is arm64 (Apple Silicon)
sysctl -n hw.memsize; sysctl vm.swapusage; df -h "$HOME"
# Windows 11 Personal, from Git Bash: confirm MSYSTEM is MINGW*, MSYS*, or UCRT*
printf 'MSYSTEM=%s\n' "$MSYSTEM"; command -v bash git curl hermes
powershell.exe -NoProfile -NonInteractive -Command '[Environment]::OSVersion.Version; (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory; Get-CimInstance Win32_PageFileUsage | Select-Object AllocatedBaseSize; Get-Volume | Select-Object DriveLetter,SizeRemaining'
# All lanes: inspect the resolved Hermes home, then CLI and gateway status
hermes --version; hermes gateway status
# Health without printing configuration: only the exit code is shown
hermes doctor >/dev/null 2>&1; printf 'hermes doctor exit=%s\n' "$?"
```

On Windows, also confirm Windows 11 and x86_64/aarch64 from system information, and that `python3` or `python` is accessible to the Personal installer. On macOS, check `command -v bash git curl python3`. Resolve `HERMES_HOME` before checking files: Linux/macOS default to `~/.hermes`, Windows to `%LOCALAPPDATA%\hermes` (convert with `cygpath -u` in Git Bash); an explicit `HERMES_HOME` takes precedence on every lane. Never print raw `hermes doctor` output or `.env`/`config.yaml` contents; they may contain secrets. Recommend an edition from the README table (default: Personal). The selected installer creates stage state when it records its first stage.

Completion: the user has seen a 3–5 line summary; a recommended edition is on the table.

### Phase 1 — Edition choice

Present a condensed editions table and your recommendation; wait for the choice. Record the choice in the session; the selected installer records its edition in stage state.

Completion: edition fixed; the gate list for that edition is prepared (see `references/gates.md`).

### Phase 2 — Environment prep

For Personal, first run `bash editions/personal/install.sh --dry-run`, then let its `prep` stage apply the matching platform policy. On Ubuntu, `prep` checks `git`, `curl`, `xz-utils`, and CA certificates, uses apt for missing packages, and handles timezone, swap and systemd sleep policy. Those apt, `/proc`, `timedatectl`, `loginctl`, and systemd operations are **Linux only**. On macOS, it checks Bash, Git, curl, Python, tar, shasum and sysctl; missing tools need manual installation. macOS manages swap; timezone and sleep/login policy need manual review. On Windows, run the installer from **Git Bash**; it checks Git for Windows Bash, Git, curl, Python, tar, `cygpath` and PowerShell. Windows manages its pagefile; timezone and power/login policy need manual review. Do not use Linux swap or systemd commands on macOS or Windows. The installer creates OAOS directories and checks secrets with mode/owner on macOS or NTFS ACLs on Windows.

Project and Company prep remains on Ubuntu LTS; follow the edition's Ubuntu apt/systemd procedure. Completion: prerequisites and directories checked, with any manual OS policy or missing tool recorded as BLOCKED rather than assumed fixed.

### Phase 3 — Install

For Personal, run `bash editions/personal/install.sh` **from the selected lane's Bash** (Git Bash on Windows). It owns the official Hermes installer download, syntax/hash check, approval gate, stage application, gate checks, and `hermes gateway install` delegation; use `--status` to inspect the Personal stages. `--dry-run` reports a plan and does not prove installation. A blocked stage exits 3 with the next action; rerun after the user completes it (`--stage <name>`; `--recheck` reruns stages already done). Project / Company stay on their Ubuntu LTS server procedures.

#### 3a. Hermes Agent

- Personal uses official `install.sh` on Ubuntu/macOS and official `install.ps1` on Windows. The Personal installer saves it (a cached copy older than a day is downloaded again), checks Bash or PowerShell syntax, prints its SHA-256, then stops for approval. **Choice gate:** show the user the SHA-256 and the saved path; add `--yes` only after the user approves. Setting `OAOS_HERMES_INSTALLER_SHA256` pins the expected hash; on Windows it is required, and `install.ps1` is refused without it. On Windows it invokes PowerShell for this official install from Git Bash; a new Git Bash session may be needed for the updated PATH. Do not run the Personal installer in PowerShell.
- For Project / Company on Ubuntu, if Hermes is absent, download the official installer, inspect it, then run it:

  ```bash
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o hermes-install.sh
  # review the downloaded file, then:
  bash hermes-install.sh
  ```

  Then open a new shell session so the updated PATH applies.
- Verify: `hermes --version` and `hermes doctor` run on the selected host.
- **Gate G1 — LLM plan.** Ask the user to pick their plan; present 2–3 options (a low-cost flat plan such as OpenCode Go is the recommended default for Personal; Nous Portal via `hermes setup --portal` is the one-command OAuth path). Walk them through the signup link. For a key, the user runs `bash bootstrap/bin/oaos-secret-set <EXACT_VARIABLE>` (names in `references/gates.md`) and picks the model with `hermes model`. Rerun `--stage llm`: it passes only when a bounded test request (`hermes chat -q "Reply with exactly: OK"`) answers, so OAuth/portal logins count and a stored key alone does not.

#### 3b. Chat platform (Personal default: Telegram)

- **Gates G2–G3 — bot token + user ID.** Guide: open `t.me/BotFather` → `/newbot` → copy the token. Then `t.me/userinfobot` → copy the numeric user ID. The user stores both in their own terminal:
  ```bash
  bash bootstrap/bin/oaos-secret-set TELEGRAM_BOT_TOKEN
  bash bootstrap/bin/oaos-secret-set TELEGRAM_ALLOWED_USERS   # numbers only, comma-separated
  ```
- Rerun `--stage telegram`: it calls Telegram `getMe` (token on curl's stdin, never in argv) and accepts only a numeric ID list.
- The `gateway` stage waits until `telegram` is done, sets `approvals.cron_mode`, `approvals.single_query_mode` and `approvals.unattended_mode` to `deny` when unset, then delegates to `hermes gateway install`; OAOS uses OS service commands only to diagnose registration. If the gateway was already running when Telegram changes, the telegram stage restarts it.
- On Linux, check the systemd user service and linger (or the explicit system service). On macOS, check launchd registration and current status. On Windows, check the Hermes ONLOGON task or its official Startup fallback and current status. Registration is read back automatically; surviving a safe reboot or logout/login stays MANUAL on every lane until observed. Verify separately that the user sends a message to the bot and gets a reply.

#### 3c. Knowledge wiki (all editions)

The installer's `wiki` stage creates `~/data/wiki` as a git repository with a seed `index.md` commit and a pre-commit hook that blocks secret-like values. An existing wiki is preserved (a non-git directory or one without commits blocks the stage). Do not create it by hand.

#### 3d. Scheduled jobs (all editions)

The installer's `cron` stage registers the daily backup of the resolved `HERMES_HOME` + the wiki as a no-agent Hermes cron job (`oaos-daily-backup`; archives under `~/.oaos/backups`, directory 700, archives 600). Personal also installs a gateway watchdog as an **OS timer** (systemd user timer on Linux, launchd agent on macOS), because Hermes cron runs inside the gateway and cannot restart it; Windows has no OAOS watchdog and relies on the Hermes ONLOGON task. Remove a legacy `oaos-gateway-watchdog` Hermes cron job if the stage reports one.

#### 3e. Harness seeding (all editions)

The installer's `harness` stage drafts `SOUL.md`, `memories/USER.md` and `memories/MEMORY.md` in the resolved `HERMES_HOME` from `harness/templates/`, writing the host facts it knows (OS, paths, gateway service) into MEMORY.md. Then run the seeding conversation from `harness/seeding.md` to replace the remaining `{{...}}` placeholders in SOUL and USER with the user (the agent maintains MEMORY.md afterward — the user reviews, not authors). The verifier reports leftover placeholders as MANUAL. Mention `AGENTS.md` only when the user has project workspaces.

#### 3f. Project edition (P1)

Project runs on one Ubuntu 22.04 or 24.04 LTS VPS with native packages and systemd only (no Docker). **G0 flow:** the user buys the VPS, installs Hermes **on that VPS**, and hands this repository URL to the agent running there; every command below runs on the VPS as one non-root administrator for whom `sudo -n true` succeeds. Follow `editions/project/README.md` and run `bash editions/project/install.sh --dry-run` first. The 13 stages are `prep hermes llm telegram wiki harness cron stack ingress mail team gateway verify`; `--status`, `--stage`, `--recheck`, `--reverify`, `--skip-verify` and `--timezone Area/City` are available. State lives in the `editions.project` namespace of `~/.oaos-install/state.json`, so a Personal run on the same host never lets Project skip a stage. Exit codes match Personal: 0 all PASS, 2 MANUAL pending, 3 blocked gate, 1 error or FAIL.

Every gate value is typed by the user in their own terminal with `bash bootstrap/bin/oaos-secret-set KEY` (Hermes `.env`, mode 600). Never ask for a value in chat, never pass one as an argument or environment variable, and never ask the user to edit a file — the installer writes every configuration file. Required gates: G0, G1, G2, G3, G6, G7, G8, G9 (8). Personal's optional G4/G5 are not offered in Project; mail is the G9 bot mailbox only.

- **G0**: `prep` reports capacity against 4 vCPU / 16 GB / 200 GB (WARN below; BLOCKED below 2 vCPU / 8 GB / 60 GB unless `OAOS_ALLOW_SMALL_HOST=1`), blocks when ports 80/443/3000/5432/6379/8065 are taken by another program, reports SSH key and password login, and adds UFW rules for SSH (22 and the current SSH port), 80 and 443 **before** enabling UFW, so the session is not cut off.
- **G1–G3**: as in Personal, checked immediately (model probe, Telegram `getMe` with the token on curl's stdin, numeric allowlist).
- **G6**: `OAOS_BASE_DOMAIN` (or `OAOS_CHAT_DOMAIN`, `OAOS_NOTE_DOMAIN`, `OAOS_PORTAL_DOMAIN`) is required. Give the user the A-record table (`chat`, `note`, `portal` → VPS public IPv4). `stack` installs PostgreSQL (pinned to `listen_addresses='localhost'`) and Redis from Ubuntu apt, Mattermost from its signed apt repository, and Outline v1.10.1 (full commit SHA pinned) from source with NodeSource Node.js 22; all listen on 127.0.0.1. `ingress` polls DNS for up to `OAOS_DNS_TIMEOUT` seconds (default 600), configures nginx and requests certificates; the ACME e-mail defaults to the G9 address unless `OAOS_ACME_EMAIL` is stored. Managed vhosts are re-rendered when the template or a domain changes; a domain change needs `OAOS_ACCEPT_DOMAIN_CHANGE=1` and `--recheck --stage stack,ingress`.
- **G9**: a dedicated bot mailbox (information-security separation from personal accounts): `OAOS_MAIL_ADDRESS`, `OAOS_MAIL_PASSWORD` (app password, no spaces), and for non-Gmail providers `OAOS_MAIL_IMAP_HOST` / `OAOS_MAIL_SMTP_HOST`. `mail` checks IMAP and SMTP logins immediately, configures Himalaya 1.x and Outline SMTP.
- **G7 (bot principle)**: a person creates the first Mattermost administrator at `https://chat.<domain>`, then creates a **bot account** for Hermes (no System Admin role) and its token. The user stores `MATTERMOST_TOKEN` and `MATTERMOST_ALLOWED_USERS` (26-character Mattermost user IDs). `team` rejects a human or admin token, checks that every allowlisted ID exists, and stores `MATTERMOST_URL`.
- **G8 (agent account)**: a person signs up at `https://note.<domain>` first (the first account is the workspace admin, via an identity provider or, for tests only, `installation.create`), turns on e-mail sign-in, and invites the AI-agent account — whose e-mail is the G9 bot mailbox — as a member or editor. The agent account signs in through the login link sent to the bot mailbox (this is why G9 SMTP comes first) and creates an API token; the user stores it as `OUTLINE_API_TOKEN`. `team` checks with `auth.info` that the token's account e-mail is the bot mailbox and is not an admin, and stores `OUTLINE_URL`. Identity-provider settings are stored as `OUTLINE_<KEY>` (for example `OUTLINE_OIDC_CLIENT_ID`) and written into `/etc/oaos/outline.env` by the installer.
- `team` restarts the gateway only when it is already installed and running; on a fresh host the `gateway` stage then starts it for the first time with Telegram and Mattermost configured, after setting `approvals.cron_mode`, `approvals.single_query_mode` and `approvals.unattended_mode` to `deny`.

Outline is pinned to v1.10.1. Its source receives exactly one loopback bind change before build. On every tag update, reapply and verify that single change, check the built server module and the live `127.0.0.1:3000` listener, and stop if any check fails. If upstream adds a supported bind-host setting, remove the patch after verifying that setting. The source build compiles `server/main.ts` into `build/server/main.js`; `build/server/index.js` is the service entry point.

The daily backup (`oaos-daily-backup`) archives the Hermes home, wiki, `~/oaos/stack`, both PostgreSQL databases, `/etc/oaos`, Mattermost config and data, and Outline data (`~/.oaos/backups/oaos-project-*.tar`, 700/600, 7 kept). `bash editions/project/restore.sh --dry-run ARCHIVE` checks an archive without changing anything; `--yes` restores.

Run `bash bootstrap/verify/project-verify.sh` (or `--json`) after the gates as the same administrator; it is also the installer's last stage. See `references/verify-checklist.md` for its rows. A real VPS end-to-end run is still required to claim P1 completion. To move a Personal wiki and harness, run `bash bootstrap/migrate/personal-to-project.sh --source user@host --dry-run` (add `--source-hermes-home PATH` when the Personal `HERMES_HOME` is not the default, or use `--source-root DIR` for a local copy), then repeat without `--dry-run`. It copies the wiki, SOUL/AGENTS, `memories/`, and merges `skills/` and `scripts/`; `--include-config` also copies `config.yaml`. The gateway is stopped during the swap, and the wiki's Personal relay remote is removed (or replaced with `--wiki-remote URL`). `.env` is excluded and credentials must be re-entered.

#### Company edition (P2)

Company extends Project with the governance layer (admin console, policy, audit, vault, permission-aware knowledge index), optional connectors and multi-LLM routing. Its installer belongs to P2; report the current status instead of claiming it is available.

### Phase 4 — Verify

Run the edition verifier and the checklist in `references/verify-checklist.md`. Every check must actually run; report only what actually passed. Personal (`bash bootstrap/verify/personal-verify.sh`, also the installer's last stage) exits 0 when all checks PASS, 2 when no check FAILs but MANUAL checks are pending, and 1 on any FAIL; the installer then records `verify` as `verified`, `applied` or `failed`. After the user has observed a safe reboot, a chat reply and an ignored non-allowed account, rerun with `--confirm reboot,chat,allowlist`. Project (`bash bootstrap/verify/project-verify.sh`) uses the same exit codes and records `verified`, `applied` or `failed` the same way; its MANUAL rows take `--confirm reboot,chat,allowlist,mm-chat,mm-deny,outline-page,mail-flow`.

### Phase 5 — Report & handoff

Report format (keep it tight):

- Edition built · bot handle / addresses · wiki path
- What to try first (link `docs/cookbook.md`)
- Backup location (`~/.oaos/backups`; `hermes backup` for full state)
- MANUAL items still pending, and who will confirm each (the verifier lists them; rerun with `--confirm` after observing them)
- Known gaps / next phase

Report the installer stage state, then point the user to `oaos-ops` for day-2.

## Gate Policy (applies to every gate)

- One gate at a time. One-line explanation of why. Wait for the result before continuing.
- **Never invent credentials.** Never echo secrets into chat, logs, or files — redact in summaries.
- Link gates: exact URL, what to click, what to copy back (if anything).
- Choice gates: 2–3 options with a recommendation.
- After each gate: verify the value works (API 200, bot replies, login succeeds) and write state.

## Pitfalls

- `hermes config set` routes `UPPER_SNAKE` names to `.env` and dotted keys to `config.yaml`. Use it for non-secret settings only (for example `approvals.cron_mode deny`); secrets go through `bootstrap/bin/oaos-secret-set`.
- Telegram is default-deny: the user must be in `TELEGRAM_ALLOWED_USERS` or paired (`hermes pairing approve telegram <code>`), or the bot will silently ignore them.
- Check Linux swap, macOS OS-managed swap, or Windows pagefile **before** heavy installs on small hosts (OOM risk); only the Linux lane creates a swapfile.
- Do not reboot without explicit approval. Registration read-back alone does not prove boot/login survival; report a pending MANUAL check when a safe reboot or logout/login was not performed.
- If `hermes skills install` is unavailable in this build, read this SKILL.md from the repository and follow it manually.
- Large files (>20 MB uploads) would need the Telegram local Bot API; OAOS does not set it up (deferred). Mention it only if asked.

## Verification

- Run every applicable item in `references/verify-checklist.md`; record PASS, FAIL, MANUAL and SKIP with command evidence. Do not claim completion while a required check is unresolved.
- Record the installer stage state; deliver the report and confirm one real bot interaction.
