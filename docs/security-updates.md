# Security Updates — Business Edition

> Audience: customer on-prem admins, OpenIT release engineering. Covers CVE intake → patch → distribution.

## 1. CVE Triage Process

| Step | Owner | SLA | Action |
|------|-------|-----|--------|
| 1. Intake | Security (security@openit.co.kr) | — | CVE detected via `pip-audit` / `trivy` nightly CI + GitHub Dependabot + vendor advisories (Postgres, Redis, nginx, Python base image). |
| 2. Classify | Security | 24h | Severity (CVSS), exploitability, blast radius (control-plane vs infra). Labels: `critical` (CVSS≥9, exploitable), `high` (7–8.9), `medium/low`. |
| 3. Decide | Security + Eng | — | `critical` → hotfix branch within 24h, backport to supported Business tags. `high` → next scheduled patch (≤14d). `medium/low` → next minor. |
| 4. Fix | Eng | per SLA | Patch deps / base image / app code; add regression test; `trivy --severity CRITICAL` must pass. |
| 5. Ship | Release | — | See §3 Update Channel. |
| 6. Notify | CS | 24h post-ship | Advisory to affected Business customers (email + portal notice) with CVE list, fixed version, upgrade command. |

### Automated checks (CI)

```bash
pip-audit --desc
trivy image open-agent-os/control-plane:latest --severity HIGH,CRITICAL
trivy fs --severity HIGH,CRITICAL .
```

Nightly workflow `.github/workflows/security-scan.yml` runs both; failure opens `security/cve-YYYY-NNNNN` issue.

### Customer local check

```bash
# Inside deployed host
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy image open-agent-os/control-plane:$(cat /opt/open-agent-os/VERSION)
pip-audit  # if running from source
```

## 2. BSL License Change

- **License of this repository:** Business Source License 1.1 (BSL 1.1), Licensor OpenIT Co., Ltd.
- **Licensed Work:** Open Agent OS (all files except where noted).
- **Additional Use Grant:** production use allowed for non-commercial evaluation/dev/test and Developer Edition; hosted/managed service or redistribution requires a commercial Business/Managed license.
- **Change Date:** **2030-08-27** for version 0.1.1 (see `LICENSE` file). Each later version carries its own Change Date ≥ 4 years after first publication.
- **Change License:** Apache License 2.0 — on the Change Date the licensed work for that version converts to Apache-2.0 automatically.
- **Effect for Business customers:** Commercial Business license is separate from BSL; BSL Change Date is the open-source fallback date, not the support EOL. Business support/EOL is per contract (typically 24 months per minor line).

> Verify current `LICENSE` file in the checkout — if Change Date differs, the file on disk wins.

## 3. Update Channel

| Channel | Tag pattern | Audience | Cadence |
|---------|------------|----------|---------|
| `latest` | `latest` | Dev | Every main merge |
| Business stable | `v1.x.y` (e.g. `v1.5.2`) | Business on-prem | Patch ≤14d, minor quarterly |
| Business hotfix | `v1.x.y-hotfixN` | Affected customers | Within 24h for critical CVE |
| LTS | `v1.5-lts` | Contract LTS | 24 months security backports |

### Distribution

- **Registry:** `ghcr.io/openit/open-agent-os/<service>:<tag>` (also mirrored to customer private registry on request).
- **Compose pin:** set `IMAGE_TAG=v1.5.2` in `.env`, then `deploy/scripts/upgrade.sh --tag v1.5.2` (rolling, health-checked, auto-rollback).
- **K8s:** `kubectl set image deployment/control-plane control-plane=ghcr.io/openit/open-agent-os/control-plane:v1.5.2 -n open-agent-os`.
- **Air-gapped:** tarball from portal (`open-agent-os-business-v1.5.2-images.tar.gz`) + `scripts/load-images.sh` + `alembic upgrade head` offline.

### Upgrade procedure (Business)

```bash
# 1. Backup (pre-upgrade)
./deploy/scripts/backup.sh

# 2. Rolling upgrade (zero-downtime via compose, auto-rollback on health fail)
./deploy/scripts/upgrade.sh --tag v1.5.2

# 3. Verify
curl -k https://localhost/healthz
./deploy/scripts/verify-16a.sh

# 4. If rollback needed (manual)
IMAGE_TAG=v1.5.1 ./deploy/scripts/upgrade.sh
# or: ./deploy/scripts/restore.sh --manifest deploy/backups/oaos-YYYYMMDD-manifest.json
```

### Notification

- Portal: https://open-agent-os.openit.co.kr/releases
- Advisory mailing list: `security-announce@openit.co.kr` (subscribe via Business contract portal)
- Severity `critical` advisories also push to customer Slack/Mattermost webhook if configured (`SECURITY_WEBHOOK_URL`).

## 4. Reporting a Vulnerability

Email `security@openit.co.kr` (PGP key on portal). Do not file public issues. Acknowledgement within 48h, fix timeline per §1 SLA. See `SECURITY.md` for full policy.

## 5. Version Support Matrix

| Line | Status | Security fixes until | Notes |
|------|--------|----------------------|-------|
| v1.5.x | Active | 2027-08-27 (or contract) | Current Business stable |
| v1.4.x | Maintenance | 2027-02-27 | Critical only |
| ≤v1.3 | EOL | — | Upgrade required |

Check `docs/architecture-v1.5.1.md` for platform requirements (Python 3.11+, Postgres 16, Redis 7).
