---
name: oaos-ops
description: "Day-2 operations for an OAOS host: check, backup, update."
version: 0.1.0
author: OpenIT (openit-ai), Hermes Agent
license: Apache-2.0
platforms: [linux]
metadata:
  hermes:
    tags: [oaos, operations, backup, maintenance]
    related_skills: [oaos-bootstrap]
---

# OAOS Ops Skill

Day-2 operations for an Open Agent OS host: health checks, backups, updates, restores, and log inspection. Run after `oaos-bootstrap` has completed (or on any OAOS host).

## When to Use

- "Check the agent / is it healthy?" — health check.
- "Back up the agent / wiki" — backup.
- "Update the agent / Hermes" — update.
- "Something broke / restore / the bot is silent" — diagnose, restore.
- Don't use for: first-time setup (use `oaos-bootstrap`); platform code development.

## Prerequisites

- An OAOS host (any edition). You can run `terminal` on it.
- Know the edition and where the wiki lives (default `~/data/wiki`).
- For destructive steps (restore, rollback), the user approves explicitly.

## Quick Reference

```bash
hermes doctor                     # overall health
hermes gateway status             # gateway service state
hermes update                     # update Hermes (pre-update backup per config)
hermes backup                     # full state backup (see docs for flags)
hermes import                     # restore from a backup
ls ~/.hermes/logs/                # gateway.log, errors.log
journalctl --user -u hermes-gateway -n 100 --no-pager   # service logs
```

## Procedure — Health Check

1. `hermes doctor` — note any failing checks; fix or report.
2. `hermes gateway status` — must be active. If not: `hermes gateway start`; if it dies again, read logs (step 4) before restarting loops.
3. Disk + memory: `df -h /`, `free -h`, `swapon --show`. Flag <10% disk free.
4. Logs: `journalctl --user -u hermes-gateway -n 100 --no-pager` and `~/.hermes/logs/errors.log` — look for repeated failures, never print secrets from logs.
5. Wiki: `git -C ~/data/wiki status --short` — flag uncommitted drift to the user.

Completion: a short status with each item PASS/FAIL + raw evidence for failures.

## Procedure — Backup

1. `hermes backup` — full agent state (respects the configured backup location).
2. Wiki: `git -C ~/data/wiki status --short && git -C ~/data/wiki push` (if a remote is configured) — or `git bundle` for a single-file archive.
3. Confirm artifacts exist: `ls -lh ~/.oaos/backups ~/.hermes/backups 2>/dev/null`.
4. For off-host copies, hand the user the artifact paths (never upload anywhere without approval).

Completion: backup artifact(s) confirmed on disk with sizes shown.

## Procedure — Update

1. Backup first (previous procedure) — always.
2. `hermes update` — applies with the configured pre-update safety (`updates.pre_update_backup`).
3. After update: `hermes doctor`, `hermes gateway status`, and a chat round-trip.
4. If something regressed: `hermes import` from the latest backup, then report.

Completion: version before/after recorded; post-update checks pass; rollback path known.

## Procedure — Restore / Diagnose Silent Bot

1. Check the gateway first (`hermes gateway status`, then service logs).
2. Common causes: token revoked, allowlist changed, provider key expired (401s in logs), network/proxy change.
3. Provider key rotation: have the user re-paste the new key; write via `hermes config set`; verify with a one-shot call. Never echo the value.
4. Full restore only with explicit approval: stop the gateway, `hermes import <backup>`, start, verify.

Completion: root cause stated with evidence (log lines), or restore verified end-to-end.

## Pitfalls

- **Never update without a fresh backup** — and never skip the post-update checks.
- Don't restart the gateway in a loop; read the logs first (a crash loop usually has one readable cause).
- Secrets: never print `.env` contents or tokens from logs into chat. Redact.
- `hermes backup` ≠ profile export: exports exclude credentials by design; a backup is the full restore path.
- Timezone/locale drift on servers quietly breaks schedules — check `timedatectl` when cron misfires.

## Verification

- Health check: all items PASS with raw evidence; failures reported with cause.
- Backup: artifact exists (size + path shown).
- Update: version bumped; doctor + gateway + round-trip pass; rollback path noted.
- Restore: service up; user confirms a live interaction.
