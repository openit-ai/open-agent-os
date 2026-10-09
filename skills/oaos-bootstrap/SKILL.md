---
name: oaos-bootstrap
description: "Bootstrap an Open Agent OS edition: install, gates, verify."
version: 0.1.3
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
hermes --version; hermes doctor; hermes gateway status
```

On Windows, also confirm Windows 11 and x86_64/aarch64 from system information, and that `python3` or `python` is accessible to the Personal installer. On macOS, check `command -v bash git curl python3`. Resolve `HERMES_HOME` before checking files: Linux/macOS default to `~/.hermes`, Windows to `%LOCALAPPDATA%\hermes` (convert with `cygpath -u` in Git Bash); an explicit `HERMES_HOME` takes precedence. Do not print `hermes doctor` output that may contain secrets. Recommend an edition from the README table (default: Personal). The selected installer creates stage state when it runs.

Completion: the user has seen a 3–5 line summary; a recommended edition is on the table.

### Phase 1 — Edition choice

Present a condensed editions table and your recommendation; wait for the choice. Record the choice in the session; the selected installer records its edition in stage state.

Completion: edition fixed; the gate list for that edition is prepared (see `references/gates.md`).

### Phase 2 — Environment prep

For Personal, first run `bash editions/personal/install.sh --dry-run`, then let its `prep` stage apply the matching platform policy. On Ubuntu, `prep` checks `git`, `curl`, `xz-utils`, and CA certificates, uses apt for missing packages, and handles timezone, swap and systemd sleep policy. Those apt, `/proc`, `timedatectl`, `loginctl`, and systemd operations are **Linux only**. On macOS, it checks Bash, Git, curl, Python, tar, shasum and sysctl; missing tools need manual installation. macOS manages swap; timezone and sleep/login policy need manual review. On Windows, run the installer from **Git Bash**; it checks Git for Windows Bash, Git, curl, Python, tar, `cygpath` and PowerShell. Windows manages its pagefile; timezone and power/login policy need manual review. Do not use Linux swap or systemd commands on macOS or Windows. The installer creates OAOS directories and checks secrets with mode/owner on macOS or NTFS ACLs on Windows.

Project and Company prep remains on Ubuntu LTS; follow the edition's Ubuntu apt/systemd procedure. Completion: prerequisites and directories checked, with any manual OS policy or missing tool recorded as BLOCKED rather than assumed fixed.

### Phase 3 — Install

For Personal, run `bash editions/personal/install.sh` **from the selected lane's Bash** (Git Bash on Windows). It owns the official Hermes installer download, syntax/hash check, approval gate, stage application, and `hermes gateway install` delegation; use `--status` to inspect `~/.oaos-install/state.json`. `--dry-run` reports a plan and does not prove installation. Project / Company stay on their Ubuntu LTS server procedures.

#### 3a. Hermes Agent

- Personal uses official `install.sh` on Ubuntu/macOS and official `install.ps1` on Windows. The Personal installer saves it, checks Bash or PowerShell syntax and SHA-256, then asks for approval before execution. On Windows it invokes PowerShell for this official install from Git Bash; a new Git Bash session may be needed for the updated PATH. Do not run the Personal installer in PowerShell.
- For Project / Company on Ubuntu, if Hermes is absent, download the official installer, inspect it, then run it:

  ```bash
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o hermes-install.sh
  # review the downloaded file, then:
  bash hermes-install.sh
  ```

  Then open a new shell session so the updated PATH applies.
- Verify: `hermes --version` and `hermes doctor` run on the selected host.
- **Gate G1 — LLM plan.** Ask the user to pick their plan; present 2–3 options (a low-cost flat plan such as OpenCode Go is the recommended default for Personal; Nous Portal via `hermes setup --portal` is the one-command path). Walk them through the signup link; have them provide the key. Set it with `hermes config set <PROVIDER>_API_KEY <value>` (UPPER_SNAKE names go to the resolved Hermes `.env`; check mode 600 on Linux/macOS or protected NTFS ACL on Windows) or use the `hermes model` wizard. Verify immediately with a one-shot call (`hermes chat -q "Reply with exactly: OK"`).

#### 3b. Chat platform (Personal default: Telegram)

- **Gates G2–G3 — bot token + user ID.** Guide: open `t.me/BotFather` → `/newbot` → copy the token. Then `t.me/userinfobot` → copy the numeric user ID.
- Configure:
  ```bash
  hermes config set TELEGRAM_BOT_TOKEN <token>
  hermes config set TELEGRAM_ALLOWED_USERS <numeric-id>
  ```
- Ask Hermes to register and start the gateway; OAOS uses OS service commands only to diagnose registration:
  ```bash
  hermes gateway install
  hermes gateway status
  ```
- On Linux, check the systemd user service and linger (or the explicit system service). On macOS, check launchd registration and current status. On Windows, check the Hermes ONLOGON task or its official Startup fallback and current status. A real logout/login or safe reboot check remains MANUAL on macOS and Windows until done on a device. Verify separately that the user sends a message to the bot and gets a reply.

#### 3c. Knowledge wiki (all editions)

```bash
mkdir -p ~/data/wiki && cd ~/data/wiki && git init -b main
# create index.md (seed: title, purpose, top-level sections), then:
git add index.md && git commit -m "wiki: seed index"
```

#### 3d. Scheduled jobs (all editions)

Register the daily backup of the resolved `HERMES_HOME` + the wiki with the Hermes cron system (see Hermes docs → Scheduled automations). Project uses this backup job only; Personal retains its existing backup and watchdog jobs. Confirm the applicable jobs appear in the cron list.

#### 3e. Harness seeding (all editions)

Run the seeding conversation from this repository's `harness/seeding.md`: draft `SOUL.md` and `USER.md` with the user; pre-fill `MEMORY.md` with environment facts (the agent maintains it automatically afterward — the user reviews, not authors). Write to the resolved `HERMES_HOME/SOUL.md` and `HERMES_HOME/memories/` (use the lane's default when unset). Use the templates in `harness/templates/` as starting points; mention `AGENTS.md` only when the user has project workspaces.

#### 3f. Project edition (P1)

On the Project VPS, follow `editions/project/README.md` and run `bash editions/project/install.sh --dry-run` first. The 13 stages are `prep hermes llm telegram wiki harness cron stack ingress mail team gateway verify`. Run `bash editions/project/install.sh` to continue; `--stage stack,ingress,mail`, `--status`, `--skip-verify`, and `--timezone Area/City` are available for recovery. Stage state is `~/.oaos-install/state.json` with `edition=project`. A blocked gate exits 3; a failed command exits 1. Keep each stage's evidence and resume only after its prerequisite is satisfied.

- **G0**: prepare an Ubuntu 22.04 or 24.04 LTS VPS (4 vCPU / 16 GB / 200 GB recommended), SSH access and root rights. `prep` installs host packages and adds OpenSSH, 80 and 443 to existing UFW rules without resetting them. Project services use apt and systemd.
- **G1–G3**: provide the LLM key, Telegram bot token and allowed user IDs as in Personal. The Project wiki, harness and daily backup mirror the Personal setup. Project starts the gateway after team configuration so both Telegram and Mattermost are present at first start.
- **G6**: set `OAOS_BASE_DOMAIN=example.com` (or `OAOS_CHAT_DOMAIN`, `OAOS_NOTE_DOMAIN`, `OAOS_PORTAL_DOMAIN` separately), and `OAOS_ACME_EMAIL`. Add A records for `chat`, `note`, `portal` pointing to the VPS public IPv4. `stack` installs PostgreSQL and Redis from Ubuntu apt, Mattermost from its signed apt repository, and pinned Outline v1.10.1 from source; systemd runs the services. `ingress` checks DNS, configures nginx and requests TLS certificates. Secrets are mode 600 in `~/oaos/stack/.env`, Mattermost config and `/etc/oaos/outline.env`; never paste values into logs or chat.
- **G9**: supply `OAOS_MAIL_ADDRESS`, `OAOS_MAIL_PASSWORD`, `OAOS_MAIL_IMAP_HOST`, `OAOS_MAIL_SMTP_HOST` from a dedicated bot mailbox. `mail` configures Himalaya 1.x when present and restarts Outline with SMTP. Confirm IMAP login and one real send/receive flow.
- **G7**: create the first Mattermost administrator at `https://chat.<domain>` in the browser. `team` checks the administrator and SiteURL through native `/opt/mattermost/bin/mmctl --local`. Local mode cannot create bots or tokens in the checked release. Create the Hermes bot and token in System Console, store `MATTERMOST_TOKEN` in the stack `.env`, and rerun `team`. The installer writes `MATTERMOST_URL` and `MATTERMOST_TOKEN` through `hermes config set` and creates `~/oaos/team-onboarding.md`. Set `MATTERMOST_ALLOWED_USERS` and test allowed and denied accounts.
- **G8**: sign in as an Outline administrator at `https://note.<domain>`, issue an API token, and verify it against the Outline API without logging the value. The token stays in Hermes `.env` for the agent's Outline API access. Outline email sign-in requires the G9 SMTP values configured earlier.

이미 게이트웨이가 실행 중인 상태에서 team 구성을 다시 적용한 경우에만, 관리자(사용자)가 별도 셸에서 게이트웨이 서비스를 재시작해야 반영됩니다.

Outline is pinned to v1.10.1. Its source receives exactly one loopback bind change before build. On every tag update, reapply and verify that single change, check the built server module and the live `127.0.0.1:3000` listener, and stop if any check fails. If upstream adds a supported bind-host setting, remove the patch after verifying that setting. The source build compiles `server/main.ts` into `build/server/main.js`; `build/server/index.js` is the service entry point.

Run `bash bootstrap/verify/project-verify.sh` (or `--json`) after the gates as the same non-root administrator who ran the installer. `sudo -n true` must succeed; the verifier uses non-interactive sudo for certificate and nginx checks. DNS, certificates, nginx, HTTPS, systemd services, Outline loopback binding, Mattermost, Outline, IMAP and Personal checks are reported separately as PASS/FAIL/MANUAL/SKIP. A real VPS end-to-end run is still required to claim P1 completion. To move a Personal wiki and harness, run `bash bootstrap/migrate/personal-to-project.sh --source user@host --dry-run`, then repeat without `--dry-run`; use `--include-config` only if its `config.yaml` should be copied. `.env` is excluded and credentials must be re-entered.

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
- Check Linux swap, macOS OS-managed swap, or Windows pagefile **before** heavy installs on small hosts (OOM risk); only the Linux lane creates a swapfile.
- Do not reboot without explicit approval. Registration read-back alone does not prove boot/login survival; report a pending MANUAL check when a safe reboot or logout/login was not performed.
- If `hermes skills install` is unavailable in this build, read this SKILL.md from the repository and follow it manually.
- Large files (>20 MB uploads) need the Telegram local Bot API — optional, note only if asked.

## Verification

- Run every applicable item in `references/verify-checklist.md`; record PASS, FAIL, MANUAL and SKIP with command evidence. Do not claim completion while a required check is unresolved.
- Record the installer stage state; deliver the report and confirm one real bot interaction.
