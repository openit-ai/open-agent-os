# Verify Checklist

Run all 12 checks on the target host with `bash bootstrap/verify/personal-verify.sh --json` (or table output). Record command results and the separate human evidence below. `--offline` records check 2 as SKIP, not PASS. The script's gateway registration read-back does not by itself prove a reboot or login survived; record that real-world check separately. Project and Company remain Ubuntu LTS server paths.

| # | Check | Common decision | Ubuntu Personal | Apple Silicon macOS Personal | Windows 11 Git Bash Personal |
|---|---|---|---|---|---|
| 1 | Hermes health | `hermes doctor` exits without blocking error | CLI in shell | CLI in macOS shell | CLI on Git Bash PATH |
| 2 | Model responds | Live `hermes chat -q "Reply with exactly: OK"` contains OK; offline is SKIP | Bounded request | Bounded request | Bounded request from Git Bash |
| 3 | Gateway service | `hermes gateway status` reports running and registration is present | systemd user unit | launchd user agent | ONLOGON task or Hermes Startup fallback |
| 4 | Boot/login survival | Registration is an automatic read-back; real persistence needs a separate MANUAL observation | systemd enabled and linger; safe reboot then status | launchd registration; logout/login then status | ONLOGON task/fallback; logoff/logon then status |
| 5 | Chat round-trip | MANUAL: allowed account sends a message and receives a reply | Same | Same | Same |
| 6 | Allowlist enforced | MANUAL: non-allowed account gets no response | Same | Same | Same |
| 7 | Wiki repository | `git -C ~/data/wiki log -1 --format=%h` finds seed commit | Native path | Native path | Git Bash path |
| 8 | Scheduled jobs | `hermes cron list` has enabled Personal backup and watchdog | Check both jobs | Check both jobs and login context | Check both jobs and login context |
| 9 | Config files | SOUL, USER, MEMORY exist without token-like text | Resolved `HERMES_HOME` (default `~/.hermes`) | Resolved `HERMES_HOME` (default `~/.hermes`) | Resolved `HERMES_HOME` (default `%LOCALAPPDATA%\hermes`) |
| 10 | Secret hygiene | No raw secrets in OAOS logs/state; separately read back Hermes `.env` protection | Owner and mode 600 | Owner and mode 600 | NTFS owner and protected ACL; `chmod` alone is insufficient |
| 11 | Backup works | Run backup once; nonempty archive under `~/.oaos/backups` | Confirm retention | Confirm archive/retention with macOS paths | Confirm archive/retention with Git Bash paths and ACLs |
| 12 | Host capacity | Target volume >10% free; RAM ≥16 GiB or swap/pagefile present | `df`, `/proc` RAM/swap | `df`, `sysctl hw.memsize`, `sysctl vm.swapusage` | `df` plus PowerShell CIM RAM/pagefile |

For checks 4–6, attach real device evidence (OS/architecture, timestamp, observed gateway status and account interaction) or leave MANUAL pending. A CI mock, `--dry-run`, service registration, or an unrun action cannot be counted as that evidence. Mark SKIP only when a check was deliberately omitted, such as `--offline`; explain the omission. Do not mark a failed or unavailable required check as SKIP.

## Project extension

Run these in addition to the 12 common checks on the Project VPS. A MANUAL result needs recorded human evidence before final completion.

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
Checks:        <PASS/FAIL/MANUAL/SKIP counts; list exceptions with redacted command evidence>
Manual:        <real device boot/login and chat evidence, or pending items and owner>
First steps:   docs/cookbook.md — try "brief me every morning at 8"
Gaps / next:   <anything unfinished, or the next phase outline>
```

If any check could not run (missing tool, blocked gate), say so explicitly in the report — never fill the gap with an assumption.
