# Managed Edition — Customer VPS Install Guide

> **Edition:** Managed (Business + VPS auto-install + remote monitoring + upgrade automation)
> **Target:** Customer-owned VPS / on-prem single host (Ubuntu 22.04 / Rocky 9) or Managed K8s
> **Stack:** `docker compose` (prod) + `nftables` + `hermes` OS user + `alembic` + health checks

## 1. Prerequisites

| Item | Requirement |
|------|-------------|
| OS | Ubuntu 22.04+ or Rocky Linux 9 (x86_64) |
| RAM / Disk | ≥ 4 GB RAM, ≥ 20 GB free |
| Network | Ports 80/443 open (for TLS), 8000-8002 internal |
| DNS | `A` record `open-agent-os.example.com` → VPS IP (if using `--domain`) |
| Deps | `docker` + `docker compose` plugin, `nftables` (`nft`), `openssl`, `python3`, `curl` |

```bash
# Ubuntu example
sudo apt update && sudo apt install -y docker.io docker-compose-plugin nftables openssl curl python3
sudo systemctl enable --now docker
```

## 2. One-Click Install (VPS)

```bash
git clone https://github.com/openit-ai/open-agent-os.git && cd open-agent-os

# Minimal (local, no TLS)
sudo ./deploy/scripts/install.sh --dry-run   # preview
sudo ./deploy/scripts/install.sh

# With domain + Let's Encrypt email (TLS)
sudo ./deploy/scripts/install.sh --domain oaos.example.com --email admin@example.com

# Non-interactive (CI / automation)
sudo ./deploy/scripts/install.sh --domain oaos.example.com --email admin@example.com --non-interactive

# Env var alternative
sudo OAOS_DOMAIN=oaos.example.com OAOS_EMAIL=admin@example.com ./deploy/scripts/install.sh --non-interactive
```

### What the installer does (8 steps)

1. **Check deps** — `docker`, `docker compose`, `nft`, `openssl`, `python3`.
2. **Create `hermes` OS user** — via `deploy/scripts/create-hermes-user.sh` (§16A.4, 0750, sudo deny, `passwd -l`).
3. **Apply nftables** — `deploy/firewall/hermes-egress.nft` → `/etc/nftables/hermes-egress.nft` + `nft -f` (§16A.6, Controlled Egress Proxy).
4. **Generate `.env`** — from `.env.example`, auto-generates `POSTGRES_PASSWORD`, `JWT_SIGNING_KEY`, `AUDIT_SIGNING_KEY`, `OAOS_ENCRYPTION_KEY` (0600). Honors `--domain`/`--email`.
5. **`docker compose up`** — `deploy/docker-compose.prod.yml` (6 services: nginx, postgres, redis, control-plane, execution-gateway, security).
6. **`alembic upgrade head`** — inside container if available, else local.
7. **Health check** — `GET /health` on 8000/8001/8002 + `deploy/scripts/health-check.sh` (services, DB, Redis, audit chain).
8. **Print URLs** — `https://<domain>` or `http://127.0.0.1:8000`.

### Flags

| Flag | Description |
|------|-------------|
| `--domain FQDN` | FQDN for ingress/TLS (e.g. `oaos.example.com`) |
| `--email ADDR` | ACME email for Let's Encrypt (requires `--domain`) |
| `--non-interactive` | Fail instead of prompting when values missing |
| `--dry-run` | Preview without executing (`OAOS_DRY_RUN=1` also) |
| `--help` | Show usage |

## 3. Verify

```bash
./deploy/scripts/health-check.sh          # text summary (PASS/FAIL/SKIP)
./deploy/scripts/health-check.sh --json   # JSON for automation
./deploy/scripts/health-check.sh --verbose

# Manual
curl -sf http://127.0.0.1:8000/health
curl -sf http://127.0.0.1:8001/health
curl -sf http://127.0.0.1:8002/health
docker compose -f deploy/docker-compose.prod.yml ps
docker compose -f deploy/docker-compose.prod.yml logs -f
```

Health check covers: **all services** (`/health`), **containers** (`docker inspect` health), **Postgres** (`pg_isready` + `SELECT 1`), **Redis** (`PING`), **audit chain** (`/v1/audit/verify` + `verify_chain` import).

## 4. Managed K8s

```bash
# Values: deploy/k8s/managed-values.yaml (ingress, TLS, resources, replicas, monitoring)

# Helm (if chart available)
helm upgrade --install oaos ./chart \
  -f deploy/k8s/managed-values.yaml \
  --namespace open-agent-os --create-namespace

# Raw manifests + kustomize patch
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/postgres-statefulset.yaml
kubectl apply -f deploy/k8s/redis-deployment.yaml
kubectl apply -f deploy/k8s/control-plane/
kubectl apply -f deploy/k8s/execution-gateway/
kubectl apply -f deploy/k8s/security/
kubectl apply -f deploy/k8s/hpa.yaml
kubectl apply -f deploy/k8s/ingress.yaml

# TLS secret (manual)
kubectl create secret tls oaos-tls --cert=tls.crt --key=tls.key -n open-agent-os
# or cert-manager: set ingress.annotations.cert-manager.io/cluster-issuer in managed-values.yaml

# Validate values
python3 -c "import yaml; yaml.safe_load(open('deploy/k8s/managed-values.yaml'))" && echo "yaml ok"
```

Key `managed-values.yaml` fields: `ingress.host` + `ingress.tls`, `replicaCount` (2/2/2 HA), `resources` (all services limits/requests §16D.1), `autoscaling` (HPA 2..10, CPU 70% §16D.3), `monitoring.enabled: true` (prometheus/grafana/alertmanager).

## 5. Upgrade & Backup (Managed automation)

```bash
# Upgrade (rolling, health-gated, auto-rollback)
./deploy/scripts/upgrade.sh --dry-run
./deploy/scripts/upgrade.sh --tag v0.2.0

# Backup (pg_dump + redis RDB + age/gpg encrypt + retention + optional S3)
./deploy/scripts/backup.sh --dry-run
./deploy/scripts/backup.sh --backup-dir /var/backups/oaos

# Cron (monitoring/upgrade automation)
# 0 2 * * * /opt/open-agent-os/deploy/scripts/backup.sh >> /var/log/oaos-backup.log 2>&1
```

Monitoring: `deploy/monitoring/prometheus.yml` + `alerts.yml` + Grafana. See `docs/deployment.md`.

## 6. Uninstall

```bash
sudo ./deploy/scripts/uninstall.sh --dry-run
sudo ./deploy/scripts/uninstall.sh --yes                # full teardown (removes volumes)
sudo ./deploy/scripts/uninstall.sh --keep-data --yes    # keep DB/Redis volumes
sudo ./deploy/scripts/uninstall.sh --keep-user --yes    # keep hermes user
```

Removes: `compose down -v` (unless `--keep-data`), `/etc/nftables/hermes-egress.nft` + `nft delete table inet hermes_egress`, `hermes` user + `/etc/sudoers.d/99-hermes-deny` (unless `--keep-user`). `.env` is kept (remove manually: `rm .env`).

## 7. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `docker: not found` | Install docker + compose plugin, `systemctl enable --now docker` |
| `nft: syntax error` | Check `nft --check -f deploy/firewall/hermes-egress.nft`; require `hermes` user exists before `nft -f` |
| `alembic upgrade` fails | Wait for postgres healthy: `docker logs oaos-postgres`; retry: `docker exec oaos-control-plane alembic upgrade head` |
| `/health` 000/404 | `docker compose ps` + `docker logs oaos-control-plane`; port conflict? `ss -tlnp \| grep 8000` |
| TLS not issuing | DNS `A` must point to VPS, port 80 open for HTTP-01 challenge |

## 8. References

- `docs/deployment.md` — general deployment (Section 5)
- `docs/deployment-verification-2026-08-27.md` — last verification report
- `deploy/k8s/managed-values.yaml` — managed K8s values (this guide §4)
- `deploy/scripts/install.sh --help` — installer flags
