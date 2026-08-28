# Managed Edition — Remote Operations Runbook

> Edition: Managed (VPS remote ops) · SLO: 99.5% control-plane uptime · p95 <500ms · backup age <48h  
> Audience: Managed central SRE + customer success · On-call runbook

## 1. Overview

Managed edition ships the same self-hosted stack (control-plane, execution-gateway, security, postgres/redis) on a customer VPS, with a **central Managed control plane** providing monitoring, backup oversight, and support tickets. Customer data stays on VPS; only metrics/logs/checkpoints (HMAC-signed) leave when `PROM_REMOTE_WRITE_URL` / `AWS_S3_BUCKET` are configured.

### Architecture at a glance

```
[VPS — docker compose]
  control-plane:8000 → prometheus scrape → remote_write → central Managed (if PROM_REMOTE_WRITE_URL)
  security:8002 → cron audit-checkpoint-to-s3.sh → S3 (versioned, signed, retention)
  backup.sh → S3 (encrypted, retention) + oaos_backup_* metrics
  admin-console → /v1/managed/* (status, support tickets, aggregated health)
       ↕
[Central Managed]  Grafana SLO dashboards + Alertmanager + ticket queue
```

---

## 2. Daily Ops Checklist

| Time | Check | Command / Where |
|------|-------|-----------------|
| 09:00 KST | SLO dashboards green (uptime 99.5, p95) | Grafana → Managed SLO folder |
| 09:05 | Unacked alerts | Alertmanager / `managed-alerts.yml` alerts |
| 09:10 | Backup age <24h (warn) / <48h (critical) | `GET /v1/managed/health` or Prometheus `oaos_backup_last_success_timestamp` |
| 09:15 | Audit checkpoint age <2h | Prometheus `oaos_audit_last_checkpoint_timestamp` or `/v1/audit/verify` |
| 09:20 | Open support tickets | `GET /v1/managed/support/tickets` (L4+) |
| Weekly | Prune old checkpoints >30d | `audit-checkpoint-to-s3.sh --prune` (also auto-pruned on upload) |
| Weekly | Verify S3 versioning enabled | `aws s3api get-bucket-versioning --bucket $AWS_S3_BUCKET` |
| Monthly | Rotate `AUDIT_SIGNING_KEY` | Rotate in SSM/ENV + restart security |

### Quick probes

```bash
# Managed status (any L4+ token)
curl -H "Authorization: Bearer $TOKEN" http://vps:3000/v1/managed/status | jq .
curl -H "Authorization: Bearer $TOKEN" http://vps:3000/v1/managed/health | jq .

# Prometheus SLO queries (instant)
# 30d avg availability
avg_over_time(up{job="control-plane"}[30d])
# p95 latency 5m
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="control-plane"}[5m])))
# backup age
time() - oaos_backup_last_success_timestamp

# Checkpoint download-verify
AUDIT_SIGNING_KEY=... AWS_S3_BUCKET=... ./deploy/scripts/audit-checkpoint-to-s3.sh --verify

# Remote write health
curl http://vps:9090/api/v1/query?query=prometheus_remote_storage_shards
```

---

## 3. Incident Response

### 3.1 Severity matrix

| Sev | Condition | Response | SLO impact |
|-----|-----------|----------|------------|
| SEV1 | control-plane `up==0` >5m, or SLO 99.5% breached (30d) | Page on-call, customer notice | Breaks 99.5% |
| SEV2 | p95 >500ms for >15m, or backup >48h, or audit chain break | Ticket + war-room within 4h | Degraded |
| SEV3 | p95 >500ms 5m spike, backup stale 24h, checkpoint stale 2h | Next-business-day ticket | Warn |

### 3.2 SEV1 — Control-plane down

1. Confirm: `up{job="control-plane"} == 0` + `GET /v1/managed/health` shows `infra.services[*].status != healthy`.
2. VPS SSH: `docker compose ps && docker compose logs --tail=200 control-plane`.
3. DB/Redis deps: `docker compose logs postgres redis; pg_isready; redis-cli ping`.
4. Mitigate: `docker compose restart control-plane` or `docker compose up -d --force-recreate`.
5. If persistent, collect: `GET /v1/managed/status`, `GET /v1/managed/health`, `docker compose logs`, and create ticket `POST /v1/managed/support/ticket` with severity `critical`.
6. Central: annotate Grafana, notify customer per SLA.

### 3.3 SEV2 — p95 latency breach

1. Query: `histogram_quantile(0.95, ...)` and `rate(http_requests_total[5m])`.
2. Check VPS resources: `node_exporter` metrics, `up` for downstream (postgres/redis).
3. Triage: slow DB queries (`pg_stat_activity`), gateway queue depth, egress DENY spikes.
4. Mitigate: scale `control-plane` replicas, restart stuck workers, tune `OAOS_*` concurrency.

### 3.4 SEV2 — Backup stale >48h

1. Check: `time() - oaos_backup_last_success_timestamp > 172800`.
2. VPS: `cat /var/log/oaos-backup.log; ls -lh deploy/backups/`.
3. S3: `aws s3 ls s3://$AWS_S3_BUCKET/$AWS_S3_PREFIX | tail`.
4. Mitigate: `BACKUP_DIR=... ./deploy/scripts/backup.sh --dry-run` then live run.
5. If S3 perms broken, verify IAM policy + bucket versioning.

### 3.5 Audit chain / checkpoint incident

1. `GET /v1/audit/verify` — `chain_valid` must be true.
2. If `AuditChainBreak`: freeze writes, dump ledger, contact security on-call. Do NOT auto-prune.
3. Checkpoint stale: check cron `crontab -l | grep audit-checkpoint`, `AUDIT_SIGNING_KEY` present, `aws s3 cp ... --verify` round-trip.

---

## 4. Backup & Restore (Managed)

- **Backup**: encrypted with `age`/`gpg` + S3 sync. Retention: daily 7d, weekly 30d (see `backup.sh`). Prometheus exposes `oaos_backup_last_success_timestamp`.
- **Checkpoint**: HMAC-SHA256 signed with `AUDIT_SIGNING_KEY` over `chain_head_hash`; uploaded to versioned S3 (`AES256`), verified via `head-object` + download-verify; `latest.json` pointer updated; old objects pruned after `CHECKPOINT_RETENTION_DAYS` (default 30d).
- **Restore rehearsal** (monthly): `deploy/scripts/restore.sh --dry-run` on staging VPS from S3 latest.

---

## 5. SLA

| Commitment | Target | Measurement | Credit |
|------------|--------|-------------|--------|
| Control-plane uptime | 99.5% monthly | Prometheus `avg_over_time(up{control-plane}[30d])` | 10% per 0.5% miss |
| p95 latency | <500ms | `histogram_quantile(0.95, ...[5m])` | Best-effort fix in 4h |
| Backup freshness | <48h | `time() - oaos_backup_last_success_timestamp` | Incident if >48h |
| Checkpoint freshness | <2h | `time() - oaos_audit_last_checkpoint_timestamp` | Warn → ticket |
| Support response | SEV1 1h / SEV2 4h / SEV3 next business day | Ticket `created_at` → first response | Per contract |

Exclusions: customer network/VPS provider outage, force majeure, customer-modified allowlists.

---

## 6. Support Tickets

- **Create**: `POST /v1/managed/support/ticket` (L4+ auth) with `title`, `body`, `severity`.
- **List**: `GET /v1/managed/support/tickets` (L4+).
- Central triage consumes aggregated health at `GET /v1/managed/health` (infra, audit, backup, SLO) to enrich tickets.

```bash
TOKEN=$(curl -s -X POST http://vps:3000/v1/auth/login -H 'Content-Type: application/json' -d '{"email":"admin@openit.co.kr","password":"..."}' | jq -r .access_token)
curl -s http://vps:3000/v1/managed/support/ticket -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"p95 breach 18:00 KST","body":"Grafana shows p95 780ms for 12m, VPS load 88%","severity":"high"}' | jq .
```

---

## 7. Configuration Reference

| Env | Default | Purpose |
|-----|---------|---------|
| `OAOS_EDITION` | `managed` | Edition label for metrics/dashboards |
| `PROM_REMOTE_WRITE_URL` | *(unset = disabled)* | Central remote_write endpoint |
| `PROM_REMOTE_WRITE_BEARER_TOKEN` | — | Bearer auth for central |
| `AUDIT_SIGNING_KEY` / `OAOS_SIGNING_KEY` | *(required)* | HMAC for checkpoint sign/verify |
| `AWS_S3_BUCKET` | *(required)* | Checkpoints + backups bucket |
| `AWS_S3_REGION` | `ap-northeast-2` | S3 region |
| `AWS_S3_PREFIX` | `audit-checkpoints/` | Checkpoint prefix |
| `CHECKPOINT_RETENTION_DAYS` | `30` | Days before S3 prune (0 = disable) |
| `CHECKPOINT_ENABLE_VERSIONING` | `1` | Auto-enable bucket versioning |
| `CHECKPOINT_VERIFY_DOWNLOAD` | `1` | Round-trip HMAC verify after upload |

### prometheus.yml integration

```yaml
rule_files:
  - alerts.yml
  - managed-alerts.yml   # SLO: 99.5% uptime, p95<500ms, backup >48h
```

Remote forwarder is **disabled by default** — merge `remote-forwarder.yml` only when `PROM_REMOTE_WRITE_URL` is set:

```bash
if [[ -n "${PROM_REMOTE_WRITE_URL:-}" ]]; then
  yq eval-all 'select(fileIndex==0) * select(fileIndex==1)' \
    deploy/monitoring/prometheus.yml deploy/monitoring/remote-forwarder.yml \
    > deploy/monitoring/prometheus.merged.yml
fi
```

---

## 8. Runbook Maintenance

- Review SLO dashboards after each release; update `managed-alerts.yml` thresholds via `promtool check rules`.
- Test this runbook quarterly with a fire-drill (stop control-plane, trigger backup stale, verify ticket flow).
- Keep `docs/managed-ops.md` and `deploy/monitoring/managed-alerts.yml` in sync.
