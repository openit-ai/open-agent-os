# Verify Checklist

Run **every** check below on the target host; record the actual output. Report only checks that actually ran and passed. Any failed check → fix or report as a gap (never mark it passed).

| # | Check | Command / method | Pass criteria |
|---|---|---|---|
| 1 | Hermes health | `hermes doctor` | Runs; no blocking errors |
| 2 | Model responds | `hermes chat -q "Reply with exactly: OK"` | Reply contains `OK` |
| 3 | Gateway service | `hermes gateway status` | Active / running |
| 4 | Boot survival | `systemctl --user is-enabled hermes-gateway`; `loginctl show-user $USER -p Linger` (user service) — or the system-service equivalent | Enabled; linger `yes` for user services |
| 5 | Chat round-trip | User sends a message to the bot from their own account | Agent replies in-chat |
| 6 | Allowlist enforced | A message from a non-allowed account is ignored | Only allowed users reach the agent |
| 7 | Wiki repository | `git -C ~/data/wiki log --oneline -1` | Seed commit exists |
| 8 | Scheduled jobs | Hermes cron list shows the backup + watchdog jobs | Both present and enabled |
| 9 | Config files (SOUL / USER / MEMORY) | `ls ~/.hermes/SOUL.md ~/.hermes/memories/` | Files exist; no secrets inside |
| 10 | Secret hygiene | `grep -rE '(sk-|ghp_|[0-9]{8,10}:[A-Za-z0-9_-]{35})' ~/.oaos/ 2>/dev/null` | No matches (logs/state contain no raw secrets) |
| 11 | Backup works | Run the backup job (or `hermes backup`) once | Backup artifact created under `~/.oaos/backups` |
| 12 | Host capacity | `df -h /`; `swapon --show`; `free -h` | >10% disk free; swap active on <16 GB RAM hosts |

## Report template

```text
Edition:       Personal | Project | Company
Bot / address: <bot handle or address>
Wiki:          ~/data/wiki (commit <short sha>)
Backups:       ~/.oaos/backups (+ hermes backup)
Checks:        12/12 passed   (list any exceptions with the raw output)
First steps:   docs/cookbook.md — try "brief me every morning at 8"
Gaps / next:   <anything unfinished, or the next phase outline>
```

If any check could not run (missing tool, blocked gate), say so explicitly in the report — never fill the gap with an assumption.
