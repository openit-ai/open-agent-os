# Verify Checklist

Personal: run `bash bootstrap/verify/personal-verify.sh --json` (or table output) on the target host; the installer's `verify` stage runs the same script on every rerun. Exit codes: **0** every check PASS (SKIP allowed), **2** no FAIL but MANUAL checks pending, **1** any FAIL. `--offline` records checks 2 and 8 as SKIP, not PASS. MANUAL rows need human evidence: after observing them, rerun with `--confirm reboot,chat,allowlist`. Project uses `bash bootstrap/verify/project-verify.sh` (see the Project extension below); Company has its own verifiers.

| # | Check | Common decision | Ubuntu Personal | Apple Silicon macOS Personal | Windows 11 Git Bash Personal |
|---|---|---|---|---|---|
| 1 | Hermes health | `hermes doctor` has no failed (✗) check; output is never printed | CLI in shell | CLI in macOS shell | CLI on Git Bash PATH |
| 2 | Model responds | Live `hermes chat -q "Reply with exactly: OK"` contains OK within 30 s; offline is SKIP | Bounded request | Bounded request | Bounded request from Git Bash |
| 3 | Gateway service | Registration present and `hermes gateway status` reports running | systemd user unit | launchd user agent | ONLOGON task or Hermes Startup fallback |
| 4 | Autostart registered | Automatic read-back of start-at-boot/login registration | unit enabled + linger yes | launchd registration | ONLOGON task/fallback |
| 5 | Reboot survival | MANUAL: safe reboot (or logout/login), then gateway status; `--confirm reboot` | Reboot | Logout/login | Logoff/logon |
| 6 | Chat round-trip | MANUAL: allowed account sends a message and receives a reply; `--confirm chat` | Same | Same | Same |
| 7 | Allowlist enforced | MANUAL: non-allowed account gets no response; `--confirm allowlist` | Same | Same | Same |
| 8 | Telegram token | `getMe` accepts `TELEGRAM_BOT_TOKEN` (token on curl stdin, never argv); offline is SKIP | Same | Same | Same |
| 9 | Allowlist configured | `TELEGRAM_ALLOWED_USERS` is a numeric ID list (values not printed) | Same | Same | Same |
| 10 | Wiki repository | `~/data/wiki` has a commit; reports whether the secret pre-commit hook is installed | Native path | Native path | Git Bash path |
| 11 | Scheduled jobs | `oaos-daily-backup` in the enabled Hermes cron list, and the OS watchdog timer active | + `oaos-gateway-watchdog.timer` | + `ai.openit.oaos.gateway-watchdog` launchd agent | backup only; no OAOS watchdog (stated in evidence) |
| 12 | Harness files | SOUL, USER, MEMORY exist, no token-like text; leftover `{{...}}` placeholders are MANUAL until seeded | Resolved `HERMES_HOME` (default `~/.hermes`) | Resolved `HERMES_HOME` (default `~/.hermes`) | Resolved `HERMES_HOME` (default `%LOCALAPPDATA%\hermes`) |
| 13 | Secret hygiene | No raw secret-like values in OAOS logs/state (backups excluded) | Same | Same | Same |
| 14 | Hermes .env protected | `.env` exists and is owner-only | Owner + mode 600 | Owner + mode 600 | NTFS owner + protected ACL; `chmod` alone is insufficient |
| 15 | Config secret-free | `config.yaml` holds no secret-like values (`${VAR}` references are fine) | Same | Same | Same |
| 16 | Unattended approvals | `approvals.cron_mode`, `approvals.single_query_mode` and `approvals.unattended_mode` are `deny` | Same | Same | Same |
| 17 | Dashboard loopback | Port 9119 (or `OAOS_DASHBOARD_PORT`) has no listener, or only loopback ones | `ss` | `lsof` | `netstat`; MANUAL if unavailable |
| 18 | Backup works | Fresh `hermes backup` archive under `~/.oaos/backups`; directory 700, archives 600 | Mode check | Mode check | Git Bash paths; NTFS profile ACLs |
| 19 | Host capacity | Target volume >10% free; RAM ≥16 GiB or swap/pagefile present | `df`, `/proc` RAM/swap | `df`, `sysctl hw.memsize`, `sysctl vm.swapusage` | `df` plus PowerShell CIM RAM/pagefile |

For checks 5–7, attach real device evidence (OS/architecture, timestamp, observed gateway status and account interaction) before using `--confirm`. A CI mock, `--dry-run`, service registration, or an unrun action cannot be counted as that evidence. Mark SKIP only when a check was deliberately omitted, such as `--offline`; explain the omission. Do not mark a failed or unavailable required check as SKIP.

## Project extension

Run with `bash bootstrap/verify/project-verify.sh` on the Project VPS, in addition to its Personal-equivalent checks. A MANUAL result needs recorded human evidence before final completion.

| # | Check | Command / method | Pass criteria |
|---|---|---|---|
| P1 | DNS | `getent ahostsv4` for chat/note/portal; compare with server public IPv4 | All three A records match |
| P2 | TLS | `openssl x509 -checkend 604800` for each certificate; certbot timer | >7 days valid; auto-renew enabled |
| P3 | nginx + HTTPS | `nginx -t`; HTTPS request to each vhost | Config valid; all three respond |
| P4 | Native services | `systemctl is-active` for PostgreSQL, Redis, Mattermost, Outline; `pg_isready`; `redis-cli ping`; `ss -ltn` | Four services active; local dependencies respond; Outline listens only on `127.0.0.1:3000` |
| P5 | Apps | Mattermost `/api/v4/system/ping`; Outline `/_health` | Both return OK |
| P6 | Bot mailbox | IMAP login using G9 values, without displaying them | Login succeeds; send/receive confirmed separately |
| P7 | Team | Allowed and denied Mattermost account interaction | Allowlist enforced; bot replies only to allowed accounts |
| P8 | Outline | Admin creates a page and verifies G8 API token | Page and API request succeed |

## Report template

```text
Edition:       Personal | Project | Company
Bot / address: <bot handle or address>
Wiki:          ~/data/wiki (commit <short sha>)
Backups:       ~/.oaos/backups (+ hermes backup)
Checks:        <PASS/FAIL/MANUAL/SKIP counts and verifier exit code; list exceptions with redacted evidence>
Manual:        <real device boot/login and chat evidence, or pending items and owner>
First steps:   docs/cookbook.md — try "brief me every morning at 8"
Gaps / next:   <anything unfinished, or the next phase outline>
```

If any check could not run (missing tool, blocked gate), say so explicitly in the report — never fill the gap with an assumption.
