# Deploy — §5 고객사 On-Prem / VPS / Private Cloud

> Architecture: `docs/architecture-v1.3.md` §5 (배포 토폴로지), §31 (signed checkpoint → S3), §16A (hardening)

## 1. 개요

| 구분 | 대상 | TLS | 고가용성 | 백업 | Audit 외부 보관 |
|------|------|-----|---------|------|-----------------|
| dev  | `docker-compose.dev.yml` | 없음 (plain HTTP) | single replica | 없음 | 로컬 ledger만 |
| prod | `docker-compose.prod.yml` | nginx sidecar (443, cert) | healthcheck + restart | `pgdata` volume + `./backups` mount | §31 signed checkpoint → S3 |

고객사 서버 / VPS / Private K8s 모두 **고객 인프라에 데이터·credential 유지** (§29, §16A.7).

---

## 2. Dev vs Prod 차이

| 항목 | dev (`docker-compose.dev.yml`) | prod (`docker-compose.prod.yml`) |
|------|-------------------------------|----------------------------------|
| **파일** | `deploy/docker-compose.dev.yml` | `deploy/docker-compose.prod.yml` |
| **서비스** | postgres, redis, control-plane, execution-gateway, security (5) | 동일 5 + **nginx (TLS termination)** |
| **포트 노출** | `5432`, `6379`, `8000/8001/8002` 전부 `ports` | DB/Redis는 `expose` 내부만, 앱 포트도 `expose`; **80/443만 `nginx`가 외부 노출** |
| **TLS** | 없음 | `deploy/nginx/nginx.conf` + `deploy/nginx/certs/tls.crt|key` (self-signed 기본, 운영 cert 교체) |
| **Volumes** | `pgdata` 1개 | `pgdata`, `redisdata`, `nginx-cache`, `*-logs` 6개 + `./backups` mount |
| **Healthcheck** | 없음 | 전 서비스 `healthcheck` (pg_isready / redis ping / HTTP /health) |
| **Resource limits** | 없음 | `deploy.resources.limits/reservations` (postgres 1C/1G, app 각 1C/1G, nginx 0.5C/256M) |
| **depends_on** | 단순 리스트 | `condition: service_healthy` |
| **Network** | default | `oaos-net` bridge isolated |
| **S3 env** | 없음 | `AWS_S3_BUCKET`, `AWS_S3_REGION`, `AWS_S3_PREFIX` (audit checkpoint §31) |
| **Restart** | 없음 (dev) | `unless-stopped` 전 서비스 |
| **Logging** | default | `json-file` with `max-size 10m` + `max-file` |
| **Secret 주입** | `env_file: ../.env` | 동일 + `POSTGRES_PASSWORD/AUDIT_SIGNING_KEY/JWT_SIGNING_KEY/OAOS_ENCRYPTION_KEY`는 `❗ required` (compose가 누락 시 에러) |

### Prod 기동

```bash
# .env 준비 (example → real)
cp .env.example .env
vi .env   # POSTGRES_PASSWORD, AUDIT_SIGNING_KEY, JWT_SIGNING_KEY, OAOS_ENCRYPTION_KEY, AWS_S3_BUCKET 등 필수

# TLS cert 교체 (운영 cert) — self-signed는 dev 검증용
cp /path/to/tls.crt deploy/nginx/certs/tls.crt
cp /path/to/tls.key deploy/nginx/certs/tls.key
chmod 600 deploy/nginx/certs/tls.key

# 기동
docker compose -f deploy/docker-compose.prod.yml up -d --build
docker compose -f deploy/docker-compose.prod.yml ps
curl -k https://localhost/healthz          # nginx
curl -k https://localhost/v1/audit/checkpoint | jq

# 검증
docker compose -f deploy/docker-compose.prod.yml config | head -n 50
```

### 검증 체크리스트

```bash
# healthcheck 전 서비스 healthy 대기
docker inspect --format '{{.State.Health.Status}}' oaos-postgres oaos-redis oaos-control-plane oaos-execution-gateway oaos-security

# TLS 확인
openssl s_client -connect localhost:443 -servername localhost </dev/null 2>&1 | grep -E "Protocol|Cipher|Certificate"

# S3 env 전달 확인
docker exec oaos-security env | grep AWS_S3
```

---

## 3. K8s 배포 (Private Cluster — 고객 소유 클러스터)

> Spec §5: Helm chart TBD — 현재는 raw manifests 제공 (`deploy/k8s/`)

### 3.1 Manifest 목록

```
deploy/k8s/
  namespace.yaml                     # Namespace: open-agent-os
  configmap.yaml                     # oaos-config (non-secret)
  secret.yaml.template               # oaos-secrets 템플릿 → 실제 secret 생성 후 적용
  postgres-statefulset.yaml          # StatefulSet + headless Service + PVC 20Gi
  redis-deployment.yaml              # Deployment + Service + PVC 5Gi
  control-plane/deployment.yaml      # Deployment (replicas 2) + probes
  control-plane/service.yaml         # ClusterIP :8000
  execution-gateway/deployment.yaml  # Deployment (replicas 2)
  execution-gateway/service.yaml     # ClusterIP :8001
  security/deployment.yaml           # Deployment (replicas 2)
  security/service.yaml              # ClusterIP :8002
  ingress.yaml                       # Ingress (nginx, TLS, host: open-agent-os.example.com)
```

### 3.2 배포 절차

```bash
# 0. namespace
kubectl apply -f deploy/k8s/namespace.yaml

# 1. ConfigMap
kubectl apply -f deploy/k8s/configmap.yaml
# S3 버킷 설정 (운영 값으로 패치)
kubectl patch configmap oaos-config -n open-agent-os -p '{"data":{"AWS_S3_BUCKET":"my-audit-bucket"}}'

# 2. Secrets — 절대 템플릿 그대로 apply 금지, 실제 값으로 생성
kubectl create secret generic oaos-secrets -n open-agent-os \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -base64 24)" \
  --from-literal=REDIS_PASSWORD="" \
  --from-literal=JWT_SIGNING_KEY="$(openssl rand -base64 32)" \
  --from-literal=AUDIT_SIGNING_KEY="$(openssl rand -base64 32)" \
  --from-literal=OAOS_SIGNING_KEY="$(openssl rand -base64 32)" \
  --from-literal=OAOS_ENCRYPTION_KEY="$(openssl rand -base64 32)" \
  --from-literal=AWS_ACCESS_KEY_ID="..." \
  --from-literal=AWS_SECRET_ACCESS_KEY="..."

# TLS secret (Ingress)
kubectl create secret tls oaos-tls -n open-agent-os --cert=tls.crt --key=tls.key
# 또는 cert-manager 사용 시 ingress.yaml의 cert-manager.io/cluster-issuer 주석 해제

# 3. Storage — storageClassName을 클라우드별로 수정 (aws: gp2, gcp: standard-rwo, on-prem: local-path)
# 필요 시: kubectl patch ... 또는 kustomize overlay에서 오버라이드

# 4. Data plane
kubectl apply -f deploy/k8s/postgres-statefulset.yaml
kubectl apply -f deploy/k8s/redis-deployment.yaml
kubectl wait --for=condition=ready pod -l app=postgres -n open-agent-os --timeout=120s
kubectl wait --for=condition=ready pod -l app=redis -n open-agent-os --timeout=60s

# 5. App plane
kubectl apply -f deploy/k8s/control-plane/
kubectl apply -f deploy/k8s/execution-gateway/
kubectl apply -f deploy/k8s/security/
kubectl rollout status deployment/control-plane -n open-agent-os
kubectl rollout status deployment/execution-gateway -n open-agent-os
kubectl rollout status deployment/security -n open-agent-os

# 6. Ingress (nginx-ingress-controller 사전 설치 필요)
kubectl apply -f deploy/k8s/ingress.yaml
kubectl get ingress -n open-agent-os
# host를 ingress host로 교체: open-agent-os.example.com → 고객 도메인
# kubectl patch ingress oaos-ingress -n open-agent-os --type=json -p '[{"op":"replace","path":"/spec/rules/0/host","value":"oaos.customer.com"}]'

# 7. 검증
kubectl get pods,svc,ingress -n open-agent-os
kubectl exec -n open-agent-os deploy/security -- wget -qO- http://localhost:8002/health
curl -k https://open-agent-os.example.com/health
curl -k https://open-agent-os.example.com/v1/audit/checkpoint | jq
```

### 3.3 이미지 레지스트리

기본 `image: open-agent-os/*:latest`는 로컬 빌드용. 운영에서는 고객 레지스트리로 교체:

```bash
# 예: ECR
docker tag open-agent-os/control-plane:latest 123456789.dkr.ecr.ap-northeast-2.amazonaws.com/open-agent-os/control-plane:v0.1.1
docker push 123456789.dkr.ecr.ap-northeast-2.amazonaws.com/open-agent-os/control-plane:v0.1.1
# k8s deployment image 필드 교체 또는 kustomize image transformer 사용
```

### 3.4 K8s → Compose 매핑

| prod compose | k8s equivalent |
|--------------|----------------|
| `nginx` sidecar | `ingress.yaml` (nginx ingress controller) |
| `volumes: pgdata` | `volumeClaimTemplates` (StatefulSet 20Gi) |
| `healthcheck` | `livenessProbe` + `readinessProbe` |
| `deploy.resources` | `resources.requests/limits` |
| `env_file` + `environment` | `ConfigMap` + `Secret` |
| `networks: oaos-net` | `ClusterIP` Services (kube DNS) |

---

## 4. S3 설정 — Audit Checkpoint 외부 보관 (§31)

### 4.1 개념

`security` 서비스의 `AuditLedger.checkpoint()`는 `chain_head_hash`를 `HMAC-SHA256(AUDIT_SIGNING_KEY)`로 서명한 `AuditCheckpoint`를 생성한다 (§31). 이를 주기적으로 S3에 업로드하면 체인 변조 시 S3 보관본과 대조해 탐지할 수 있다.

### 4.2 환경 변수

| 변수 | 필수 | 설명 | Compose | K8s |
|------|------|------|---------|-----|
| `AWS_S3_BUCKET` | ✅ (prod) | 버킷 이름 | `.env` → compose `environment` | `ConfigMap` `AWS_S3_BUCKET` |
| `AWS_S3_REGION` |  | 리전 (default `ap-northeast-2`) | `.env` | `ConfigMap` |
| `AWS_S3_PREFIX` |  | 키 prefix (default `audit-checkpoints/`) | `.env` | `ConfigMap` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | ✅ (IAM role 아닌 경우) | S3 쓰기 credential | `.env` / IAM role | `Secret` |
| `AUDIT_SIGNING_KEY` | ✅ | HMAC 서명 키 (checkpoint 서명/검증) | `.env` | `Secret` |

> **권장:** EKS/ECS에서는 IRSA(IAM Roles for Service Accounts) 사용 — `AWS_*` credential을 Secret에 넣지 않고 ServiceAccount에 role 부여.

### 4.3 S3 버킷 준비

```bash
aws s3 mb s3://my-audit-bucket --region ap-northeast-2
aws s3api put-bucket-versioning --bucket my-audit-bucket --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket my-audit-bucket \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket my-audit-bucket \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
# lifecycle: checkpoint 영구 보관, 버전 관리 권장
```

### 4.4 업로드 스크립트

`deploy/scripts/audit-checkpoint-to-s3.sh` — `GET /v1/audit/checkpoint` → 서명 검증 → S3 업로드 (§31)

```bash
# 수동 실행
AUDIT_SIGNING_KEY="$(grep AUDIT_SIGNING_KEY .env | cut -d= -f2)" \
AWS_S3_BUCKET=my-audit-bucket \
SECURITY_URL=http://localhost:8002 \
  bash deploy/scripts/audit-checkpoint-to-s3.sh

# 크론 (매시간 정각) — 권장: systemd timer 또는 K8s CronJob
# crontab -e
# 0 * * * * AUDIT_SIGNING_KEY=... AWS_S3_BUCKET=... SECURITY_URL=http://security:8002 /opt/open-agent-os/deploy/scripts/audit-checkpoint-to-s3.sh >> /var/log/oaos-audit-checkpoint.log 2>&1

# K8s CronJob 예시 (별도 파일로 생성 가능)
# kubectl create cronjob oaos-audit-checkpoint --image=curlimages/curl --schedule="0 * * * *" -- /bin/sh /scripts/audit-checkpoint-to-s3.sh
```

업로드 키 형식: `audit-checkpoints/YYYY/MM/DD/HHMMSS-<head8>.json` + `audit-checkpoints/latest.json` (덮어쓰기)

### 4.5 검증 (복구 시)

```bash
# S3에서 최신 checkpoint 조회
aws s3 cp s3://my-audit-bucket/audit-checkpoints/latest.json /tmp/checkpoint.json
cat /tmp/checkpoint.json | jq

# 서명 재검증 (AUDIT_SIGNING_KEY 필요)
python3 -c "
import json, hmac, hashlib, os
cp=json.load(open('/tmp/checkpoint.json'))
key=os.environ['AUDIT_SIGNING_KEY']
exp=hmac.new(key.encode(), cp['chain_head_hash'].encode(), hashlib.sha256).hexdigest()
print('OK' if hmac.compare_digest(exp, cp['signature']) else 'TAMPER DETECTED')
"

# 현재 ledger와 대조
curl -s http://security:8002/v1/audit/verify | jq
curl -s http://security:8002/v1/audit/checkpoint | jq
```

### 4.6 대체 스토리지

S3 호환 스토리지(MinIO, Ceph, NCP Object Storage) 사용 시 `AWS_ENDPOINT_URL_S3` (aws cli v2: `--endpoint-url`)로 오버라이드 가능.

---

## 5. Systemd Production (non-Docker) — OAOS

> Docker and systemd are **parallel, separate distributions** sharing the same code.
> Docker keeps its path `deploy/docker-compose.*.yml` + `.env` **unchanged**.
> Systemd uses a **separate** unified env template `config/oaos.env.example` (also at `deploy/systemd/oaos.env.example`) and units under `deploy/systemd/`.

### 5.1 위치 및 분리 원칙

| 구분 | 설정 파일 | 설치 경로 | 프리플라이트 |
|---|---|---|---|
| **Docker** | `.env` (root) + `deploy/docker-compose.*.yml` | `docker compose -f deploy/docker-compose.prod.yml up -d` | `docker compose config` |
| **systemd (OAOS)** | `config/oaos.env.example` → `config/oaos.env` or `/etc/oaos/oaos.env` | `deploy/systemd/*.service` → `/etc/systemd/system` or `~/.config/systemd/user` | `bash scripts/check-production-config.sh --env-file <path>` |
| **K8s** | `deploy/k8s/configmap.yaml` + `secret.yaml.template` | `kubectl apply -f deploy/k8s/` | same check script |

- 공통 코드 변경 없이 배포별 설정을 분리합니다. Docker 경로 회귀는 `pytest tests/test_deploy_hardening.py` 등으로 검증합니다.

### 5.2 OAOS systemd 유닛 (Control Plane 8100 + optional)

| Unit | 포트 | 진입점 (검증됨) | WorkingDirectory | 설명 |
|---|---|---|---|---|
| `oaos-control-plane.service` (system) | 8100 | `uvicorn control_plane.app:app` | `.../control-plane` | **Primary** — 현재 운영 192.168.6.61 `oaos-control-plane.service:8100` 과 동일. Mattermost Adapter Gateway. |
| `user/oaos-control-plane.service` (user) | 8100 | 동일 | `%h/open-agent-os/control-plane` | `systemctl --user` 용 — sudo 없이 개발/운영 (production user unit과 1:1 매칭) |
| `oaos-execution-gateway.service` | 8001 | `uvicorn execution_gateway.app:app` | `.../execution-gateway` | Optional — `--with-optional` 로만 설치, entrypoint 검증 후. |
| `oaos-security.service` | 8002 | `uvicorn app:app` (security/app.py) | `.../security` | Optional — `--with-optional` 로만 설치, entrypoint 검증 후. |

- Admin API (`:8010`, `oaos-admin-api.service` root) 와 Admin Console (`:3012`) 는 별도 systemd units로 운영 중이며 본 패키지에서 다루지 않습니다.
- 모든 unit은 `WorkingDirectory`, `PYTHONPATH`, `EnvironmentFile`, `ExecStart` 가 `install-systemd.sh` 에 의해 실제 `REPO_ROOT` 와 `PYTHON_BIN` 로 패치됩니다.

### 5.3 빠른 시작 (systemd — 비-Docker)

```bash
# 0) 템플릿 → 실제 env (절대 커밋 금지, 0600 필수, CHANGE_ME_* 모두 교체)
cp config/oaos.env.example config/oaos.env
chmod 600 config/oaos.env
vi config/oaos.env  # openssl rand -base64 32 로 생성한 값으로 교체

# 1) 프리플라이트 — 누락/placeholder/취약 값 + 파일 위치를 친절히 안내, secret 미출력
bash scripts/check-production-config.sh --env-file config/oaos.env
# 또는 wrapper
bash deploy/systemd/check-production-config.sh --env-file config/oaos.env

# 실패 시 예시 (secret 미출력):
#   [ERROR] Missing or placeholder: JWT_SIGNING_KEY — file: config/oaos.env (len=8)
#     → Fix: set JWT_SIGNING_KEY in config/oaos.env (≥32 chars, not CHANGE_ME)
#        Generate: openssl rand -base64 32

# 2) 설치 — user (sudo 불필요, 192.168.6.61 운영 user unit과 동일 :8100)
bash deploy/systemd/install-systemd.sh --user --env-file config/oaos.env --dry-run
bash deploy/systemd/install-systemd.sh --user --env-file config/oaos.env
systemctl --user status oaos-control-plane.service
curl -s http://127.0.0.1:8100/healthz | jq
journalctl --user -u oaos-control-plane -f

# 3) 시스템 전역 (sudo 필요, /etc/oaos/oaos.env 사용)
sudo mkdir -p /etc/oaos
sudo cp config/oaos.env.example /etc/oaos/oaos.env
sudo chmod 600 /etc/oaos/oaos.env && sudo vi /etc/oaos/oaos.env
bash scripts/check-production-config.sh --env-file /etc/oaos/oaos.env
sudo bash deploy/systemd/install-systemd.sh --env-file /etc/oaos/oaos.env
# optional 게이트웨이/보안까지
sudo bash deploy/systemd/install-systemd.sh --env-file /etc/oaos/oaos.env --with-optional
systemctl status oaos-control-plane.service
curl -s http://127.0.0.1:8100/healthz | jq

# 4) 검증 (공통)
curl -s http://127.0.0.1:8100/healthz   # liveness 200
curl -s http://127.0.0.1:8100/readyz    # readiness (prod 503 on degraded)
curl -s http://127.0.0.1:8100/v1/health/detailed | jq
systemd-analyze verify /etc/systemd/system/oaos-control-plane.service
```

### 5.4 Docker vs systemd 선택 가이드

- **Docker를 선택** — 컨테이너 격리, nginx TLS sidecar, `pgdata` volume, S3 checkpoint을 그대로 쓰려는 경우. 기존 `deploy/docker-compose.*.yml` 경로는 그대로 유지됩니다.
- **systemd를 선택** — VM/베어메탈에 네이티브로 올리고, 호스트의 systemd, PostgreSQL, Redis를 직접 쓰려는 경우. `config/oaos.env.example` 과 `scripts/check-production-config.sh` 로 친절한 프리플라이트를 제공합니다.
- **혼용 금지** — 같은 호스트에서 Docker와 systemd를 동시에 같은 포트(8100/8001/8002)로 띄우면 충돌합니다. 한 가지 배포 방식을 선택하세요.

### 5.5 Systemd / Firewall — Hermes (P0, 별도)

- `deploy/systemd/` — `hermes.service` (§16A.4~16A.6) — 전용 OS 계정, filesystem/network isolation
- `deploy/firewall/` — `hermes-egress.nft` / `firewalld-alternative.md`
- 상세 절차: `deploy/systemd/README.md` 참조

---

## 6. 관련 문서

- `docs/architecture-v1.3.md` §5, §16A, §31
- `deploy/docker-compose.dev.yml` / `deploy/docker-compose.prod.yml`
- `deploy/k8s/README.md` (K8s quick ref)
- `deploy/scripts/audit-checkpoint-to-s3.sh`
- `deploy/nginx/nginx.conf`
