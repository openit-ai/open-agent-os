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

Run `bash bootstrap/verify/project-verify.sh` (`--json`, `--offline`) on the Project VPS as the non-root administrator who ran the installer; the installer's `verify` stage runs it on every rerun. It reuses Personal's decisions for Hermes health, model, gateway service and autostart, Telegram token and allowlist, wiki, harness files, secret hygiene, Hermes `.env` protection, `config.yaml` and unattended approvals, and adds the rows below. Exit codes match Personal: **0** all PASS, **2** no FAIL but MANUAL pending, **1** any FAIL. `--offline` turns the model request, Telegram `getMe`, public DNS, HTTPS and mail logins into SKIP. Non-interactive sudo is used only for root-only reads (certificates, `nginx -t`, `ufw status`, file modes, `SHOW listen_addresses`) and the backup job's database dump.

| Row | Check | Pass criteria |
|---|---|---|
| Gateway telegram / mattermost | Hermes `gateway_state.json` platform state | Both `connected` |
| Scheduled jobs | `oaos-daily-backup` in the cron list; script equals `editions/project/backup.sh` | Registered and current |
| Backup works | Runs the real backup job | Archive 600 in a 700 directory; manifest lists both databases and `/etc/oaos` + service data |
| Restore dry run | `editions/project/restore.sh --dry-run` on that archive | Every part readable |
| Host capacity | `df`, `/proc`, G0 sizing | Disk >10% free, swap rule met, not below 2 vCPU / 8 GB / 60 GB (below 4/16/200 is PASS with a WARN note) |
| Firewall | `ufw status` | Active; 22 (or OpenSSH), 80, 443 allowed |
| DNS / TLS / HTTPS ×3 | `getent ahostsv4` vs public IPv4; `openssl x509 -checkend 604800`; HTTPS request | Each of chat, note, portal |
| TLS auto-renew, nginx config | certbot timer; `nginx -t` | Enabled; valid |
| Native services | `systemctl is-active` PostgreSQL, Redis, Mattermost, Outline; `pg_isready`; `redis-cli ping` | All respond |
| Loopback :5432 / :6379 / :8065 / :3000 | `ss -ltn` | Listener present and loopback only |
| PostgreSQL bind | `SHOW listen_addresses` | `localhost` |
| Mattermost ping, Outline health | `/api/v4/system/ping`; `/_health` | OK |
| Service secret files | `stat` | `~/oaos/stack/.env` 600 (installer user), `/etc/oaos/outline.env` 600 root, Mattermost `config.json` 600 mattermost |
| Stack gate secrets | `~/oaos/stack/.env` | No `MATTERMOST_TOKEN`, `OUTLINE_API_TOKEN`, `OAOS_MAIL_PASSWORD` copy |
| Mattermost bot | `/api/v4/users/me` with `MATTERMOST_TOKEN` (stdin config) | `is_bot` true, no `system_admin` |
| Mattermost allowlist | `MATTERMOST_ALLOWED_USERS`; `/api/v4/users/ids` | 26-character IDs that all exist |
| Outline agent token | `POST /api/auth.info` with `OUTLINE_API_TOKEN` | Account e-mail = `OAOS_MAIL_ADDRESS`; role not admin |
| Outline URL | `OUTLINE_URL` in the Hermes `.env` | `https://<note domain>` |
| Bot mailbox IMAP / SMTP | Login with the G9 values, password never printed | Login succeeds; a failed login with values present is FAIL; absent values are FAIL |
| MANUAL: Reboot survival, Telegram chat / allowlist, Mattermost chat / deny, Outline page, Mail flow | Human evidence | Rerun with `--confirm reboot,chat,allowlist,mm-chat,mm-deny,outline-page,mail-flow` after observing each |

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
