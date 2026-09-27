# START-HERE — Agent Bootstrap Procedure

> **For the agent — not the human.** When a user hands you this repository's URL (e.g. *"set up openit-ai/open-agent-os"*), run this procedure top to bottom. The human only handles the gates (§5).
> 한국어: 이 문서는 **에이전트용**입니다. 사용자가 이 저장소 URL을 건네면 아래 절차를 순서대로 실행하세요. 사람은 게이트(§5)만 처리합니다.

## 0. Preconditions

- Hermes Agent installed and tool-capable (`terminal`, `read_file`, `write_file` available).
- You are on the machine that will host the agent (confirm with the user).
- Network reachable: GitHub, the official Hermes installer, and the LLM provider endpoint; Ubuntu Project/Company also need OS package sources.

## 1. Recon — read-only

1. Read `README.md` (§2 editions) and this file completely.
2. Identify OS/architecture with `uname -a`. Ubuntu: `/etc/os-release`, `nproc`, `free -h`, `df -h /`, `swapon --show`. Apple Silicon macOS: confirm `uname -m` is `arm64`; inspect `sysctl -n hw.memsize`, `sysctl vm.swapusage`, and `df -h "$HOME"`. Windows 11: run from **Git Bash**, confirm `MSYSTEM`, Git Bash and `hermes` PATH; use PowerShell only for read-only OS version, RAM, pagefile and volume queries (see bootstrap skill Phase 0).
3. Check the resolved Hermes home, `hermes --version`, and gateway status if present. Linux/macOS default to `~/.hermes`; Windows defaults to `%LOCALAPPDATA%\hermes` unless `HERMES_HOME` overrides it. Do not print secret files.
4. Report findings to the user in 3–5 lines; flag anything that fails the edition requirements (README §2).

Completion: you can state OS, resources, Hermes presence, and a recommended edition.

## 2. Edition choice

- Present a condensed README §2 comparison and your recommendation (default: Personal).
- Wait for the user's choice. Keep it in the session; the selected edition installer writes stage status to `~/.oaos-install/state.json`.

Completion: user picked an edition; state file written.

## 3. Install the bootstrap skill

```bash
hermes skills install openit-ai/open-agent-os/skills/oaos-bootstrap
# fallback: raw URL form (see README §5)
```

If skill installation is unavailable in this Hermes build, read `skills/oaos-bootstrap/SKILL.md` from this repository and follow it manually.

Completion: `oaos-bootstrap` appears in the skills list, or its SKILL.md content is loaded.

## 4. Run the bootstrap skill

From here the **skill is the procedure of record** (`skills/oaos-bootstrap/SKILL.md`): environment prep → install → gates → verify → report. Its phases, gates, and completion criteria supersede any summary in this file.

For Personal, use `bash editions/personal/install.sh --dry-run` and then the installer from the selected OS Bash (Git Bash on Windows); run `bash bootstrap/verify/personal-verify.sh` afterward. Ubuntu apt, `/proc`, systemd and linger steps are Linux only. macOS uses the official `install.sh` and launchd; Windows uses the official `install.ps1` through Git Bash and an ONLOGON task/Startup fallback. Scripts cover all three lanes, local mocks passed, and the three-runner CI matrix is green (2026-09-27); real-device installation/login checks are pending. A registered service does not by itself prove reboot or login survival.

For Project, use `bash editions/project/install.sh --dry-run` before the real install and `bash bootstrap/verify/project-verify.sh` afterward. Run the installer and verifier as the same non-root administrator; `sudo -n true` must succeed for host changes and privileged certificate/nginx checks. Project runs PostgreSQL, Redis, Mattermost, and Outline as native systemd services. See `editions/project/README.md` for the domain and mailbox inputs, source patch check, and Personal migration command.

## 5. Gates — what the human does

A gate is one small interaction: **open a link**, **choose an option**, or **paste a value**. Rules:

- One gate at a time; explain why in one line; wait for the result before continuing.
- Never invent credentials; never echo secrets into chat, logs, or docs; verify every pasted value immediately (API returns 200, login succeeds, etc.).
- Link gates: give the exact URL, what to click, and what to copy back (if anything).
- Choice gates: give 2–3 options with a recommendation.

Completion: gate result verified; state file updated.

## 6. Verify & report

- Run the verify checklist (skill §Verification; `bootstrap/verify` when available).
- Report: edition built · addresses/URLs · what to try first (link to cookbook) · backup location · known gaps.
- Do not report success for any check that did not actually run and pass.

## 7. Resume, failure, rollback

- **Resume:** read `~/.oaos-install/state.json`; continue from the last incomplete stage. Re-running completed stages must be safe (idempotent) — check before re-applying.
- **Failure:** fix forward when safe; if a step is destructive, irreversible, or externally blocked, stop and report — never guess a workaround on production data.
- **Rollback:** for everything you change, know the undo (package remove, service disable, file restore). Take backups before destructive steps.

Completion: state consistent; user informed of any unfinished phase.

## References

- `skills/oaos-bootstrap/SKILL.md` — canonical orchestrator (phases, gates, checks)
- `docs/architecture-v2.0.md` — design rationale (why it works this way)
- `docs/cookbook.md` · `docs/faq.md` — after setup
- `harness/` — agent configuration files & seeding (SOUL / USER / MEMORY / AGENTS)
