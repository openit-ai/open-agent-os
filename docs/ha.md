# HA 운영 가이드 — Open Agent OS v1.6.4

> 대상: `deploy/docker-compose.prod.yml` + `deploy/k8s/` (replicas: 2, PDB, affinity)

## 1. Healthcheck / Probe

### Docker Compose (prod)
각 app 서비스(`control-plane` :8000, `execution-gateway` :8001, `security` :8002)는 동일한 스펙으로 healthcheck를 가진다:

```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:<PORT>/healthz"]
  interval: 30s
  timeout: 5s
  retries: 3
  start_period: 10s
restart: unless-stopped
deploy:
  resources:
    limits: { cpus: "1.0", memory: 1G }
    reservations: { cpus: "0.5", memory: 512M }
```

- `curl -f`는 2xx가 아니면 non-zero 종료 → Compose가 `unhealthy`로 마킹
- `depends_on: condition: service_healthy`로 nginx는 3개 app이 healthy 이후에만 기동
- `restart: unless-stopped`로 호스트 재부팅/프로세스 크래시 시 자동 재시작
- `GET /healthz`(liveness) / `GET /readyz`(readiness) / `GET /health`(legacy) 모두 200을 반환하고, `/readyz`는 DB/Redis degraded 시에도 200을 반환하되 `checks` 필드로 상태를 노출(fail-open)

### Kubernetes
```yaml
replicas: 2
strategy: { type: RollingUpdate, maxUnavailable: 1, maxSurge: 1 }
livenessProbe:  { httpGet: { path: /healthz, port: 8000 }, initialDelaySeconds: 10, periodSeconds: 30, failureThreshold: 3 }
readinessProbe: { httpGet: { path: /readyz,  port: 8000 }, initialDelaySeconds: 10, periodSeconds: 10, failureThreshold: 3 }
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        labelSelector: { matchLabels: { app: control-plane } }
        topologyKey: kubernetes.io/hostname
```

- `livenessProbe /healthz` 실패 시 kubelet이 컨테이너 재시작
- `readinessProbe /readyz` 실패 시 Service Endpoints에서 제외 (트래픽 차단)
- `affinity: podAntiAffinity(hostname)`로 동일 서비스 Pod을 다른 노드에 분산 (단일 노드 장애 시 최소 1 replica 생존)
- `PodDisruptionBudget(minAvailable: 1)`로 `kubectl drain`/`upgrade` 등 자발적 중단 시 최소 1 Pod 유지 — `deploy/k8s/pdb.yaml` 적용 필요

```bash
kubectl apply -f deploy/k8s/pdb.yaml
kubectl get pdb -n open-agent-os
```

### 엔드포인트 매핑

| 프로브 | 경로 | 의미 | 실패 시 |
|--------|------|------|---------|
| liveness | `/healthz` | 프로세스 생존 | 재시작 |
| readiness | `/readyz` | 트래픽 받을 준비 | Endpoints 제외 |
| detailed | `/v1/health/detailed` | DB/Redis/latency 상세 | 관측용 (200 고정) |

## 2. 재시도 (Retry)

- **Compose healthcheck 재시도**: `retries: 3` — 3회 연속 실패 시 `unhealthy`. `start_period: 10s` 동안 실패는 카운트 제외(콜드 스타트 허용)
- **K8s probe 재시도**: `failureThreshold: 3` × `periodSeconds` (liveness 30s → 90s 후 재시작, readiness 10s → 30s 후 트래픽 차단)
- **클라이언트 재시도**: Ingress/nginx ↔ app 간 5xx/네트워크 오류 시 클라이언트가 지수 백오프(예: 100ms, 300ms, 900ms, max 3회)로 재시도. 멱등한 `GET /healthz, /readyz`와 `POST /v1/*`는 `request_id`/`Idempotency-Key`로 중복 방지
- **큐 드레이닝**: `execution-gateway`는 `SIGTERM` 시 `_shutting_down=true`로 전환하고 `terminationGracePeriodSeconds: 30` 동안 활성 요청을 드레이닝한 뒤 종료. `/readyz`는 draining 중 `draining` 상태를 반환해 readiness 실패로 트래픽을 차단

## 3. 무중단 배포 (Zero-Downtime)

### Compose
```bash
docker compose -f deploy/docker-compose.prod.yml pull
docker compose -f deploy/docker-compose.prod.yml up -d --no-deps --build control-plane
docker compose -f deploy/docker-compose.prod.yml up -d --no-deps --build execution-gateway
docker compose -f deploy/docker-compose.prod.yml up -d --no-deps --build security
# 검증
docker inspect --format '{{.State.Health.Status}}' oaos-control-plane oaos-execution-gateway oaos-security
curl -f http://localhost:8000/healthz && curl -f http://localhost:8001/healthz && curl -f http://localhost:8002/healthz
```
- 한 서비스씩 순차 교체 + `healthcheck`로 다음 서비스 진행을 게이트. `nginx`는 `depends_on: service_healthy`이므로 교체 중에도 healthy replica로 라우팅 유지

### Kubernetes
```bash
kubectl set image deployment/control-plane -n open-agent-os control-plane=open-agent-os/control-plane:v1.6.5
kubectl rollout status deployment/control-plane -n open-agent-os --timeout=120s
kubectl set image deployment/execution-gateway -n open-agent-os execution-gateway=open-agent-os/execution-gateway:v1.6.5
kubectl rollout status deployment/execution-gateway -n open-agent-os --timeout=120s
kubectl set image deployment/security -n open-agent-os security=open-agent-os/security:v1.6.5
kubectl rollout status deployment/security -n open-agent-os --timeout=120s
# 실패 시 롤백
kubectl rollout undo deployment/control-plane -n open-agent-os
```
- `RollingUpdate(maxUnavailable: 1, maxSurge: 1)` + `replicas: 2` + `readinessProbe` 조합으로 항상 최소 1 Pod가 Ready 상태를 유지
- `PDB(minAvailable: 1)`가 `kubectl drain` 중에도 최소 가용성을 보장
- `podAntiAffinity`로 신규 Pod이 기존 Pod과 다른 노드에 스케줄되므로 노드 단위 롤링 재시작 시에도 가용성 유지
- HPA(`deploy/k8s/hpa.yaml`, min 2 ~ max 10)는 롤아웃과 독립적으로 동작 — 롤아웃 중에도 CPU 70%/메모리 80% 기준 스케일

### 검증 체크리스트
```bash
# Compose
docker compose -f deploy/docker-compose.prod.yml config | grep -A4 healthcheck
docker compose -f deploy/docker-compose.prod.yml ps

# K8s
kubectl get deployments -n open-agent-os -o wide
kubectl get pods -n open-agent-os -o wide
kubectl describe pdb -n open-agent-os
kubectl get events -n open-agent-os --sort-by=.lastTimestamp | tail -20
for svc in control-plane execution-gateway security; do
  port=$(kubectl get svc $svc -n open-agent-os -o jsonpath='{.spec.ports[0].port}')
  kubectl exec -n open-agent-os deploy/$svc -- wget -qO- http://localhost:$port/healthz
  kubectl exec -n open-agent-os deploy/$svc -- wget -qO- http://localhost:$port/readyz
done
```
