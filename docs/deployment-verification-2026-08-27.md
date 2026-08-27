# Deployment Verification Report — 2026-08-27

> **검증일**: 2026-08-27 19:55 KST (Rocky Linux 9.8) · **커밋**: `ea44ace5` (P2) · **테스트**: 172 passed

## 1. Summary

| 영역 | 결과 | 비고 |
|------|------|------|
| docker-compose.prod.yml | ✅ yaml ok | 6 services, oaos-net, 6 volumes — docker/podman 미설치로 `compose config` 생략 |
| K8s manifests (13 files) | ✅ yaml ok | Namespace/ConfigMap/StatefulSet/Deployments/Services/Ingress/HPA 3 docs 전부 valid |
| nginx (deploy/nginx/nginx.conf) | ✅ ok | 80→443 redirect, TLS 1.2/1.3, rate limit 20r/s, 3 upstreams — 호스트 nginx cert 오류는 별도 사이드카와 무관 |
| systemd (hermes.service) | ✅ 7 directives | NoNewPrivileges/ProtectSystem/PrivateTmp 등 — `systemd-analyze verify` placeholder 경고만 |
| firewall (hermes-egress.nft) | ✅ ok | INTERNAL_DB_NET 10.20.0.0/16, ERP/CRM DENY, ACP 8000 ALLOW, DROP default |
| scripts | ✅ bash -n ok | audit-checkpoint-to-s3.sh, verify-16a.sh, create-hermes-user.sh |
| App smoke (FastAPI) | ✅ 5/5 pass | health→session→prompt→context(trace 일치)→cross-user 403 |
| Tests | ✅ 172 passed | P2 신규 33 (performance 14 + e2e 19) 포함 |

## 2. Static Checks

```
services: ['nginx', 'postgres', 'redis', 'control-plane', 'execution-gateway', 'security']
networks: ['oaos-net']
volumes: ['pgdata', 'redisdata', 'nginx-cache', 'control-plane-logs', 'execution-gateway-logs', 'security-logs']
```

```
[OK] configmap.yaml: 1 doc — ConfigMap/oaos-config
[OK] control-plane/deployment.yaml: 1 doc — Deployment/control-plane
[OK] control-plane/service.yaml: 1 doc — Service/control-plane
[OK] execution-gateway/deployment.yaml: 1 doc — Deployment/execution-gateway
[OK] execution-gateway/service.yaml: 1 doc — Service/execution-gateway
[OK] hpa.yaml: 3 docs — HPA/control-plane-hpa, execution-gateway-hpa, security-hpa
[OK] ingress.yaml: 1 doc — Ingress/oaos-ingress
[OK] postgres-statefulset.yaml: 2 docs — Service/postgres, StatefulSet/postgres
[OK] redis-deployment.yaml: 3 docs — Service/redis, Deployment/redis, PVC/redisdata
[OK] security/deployment.yaml: 1 doc — Deployment/security
[OK] security/service.yaml: 1 doc — Service/security
```

## 3. App Smoke (without containers)

| Step | Request | Result |
|------|---------|--------|
| health | `GET /health` | 200 `{"status":"ok"}` |
| create_session | `POST /v1/sessions` kim | 200 session_id + trace_id |
| prompt | `POST /v1/sessions/{sid}/prompt` | 200 trace 일치 (ACP 미기동 → queued_local fallback) |
| context | `GET /v1/context/{sid}` | 200 trace 일치 |
| cross-user | `GET /v1/sessions/{sid}` lee | 403 blocked |

## 4. verify-16a.sh (dry-run on this host)

- `hermes user` / `hermes.service` / `nft ruleset` — **FAIL (expected)** — 아직 호스트에 미설치, 파일 기반 검증은 PASS
- `hermes-egress.nft` / `hermes.service` 내용 검증 — **PASS** (INTERNAL_DB_NET, 5432 DROP, ACP 8000 ALLOW, 7 systemd directives)

실 배포 시 순서: `create-hermes-user.sh` → `hermes.service` 설치 → `hermes-egress.nft` 로드 → `docker`/`podman` 설치 후 `compose up`.

## 5. Tests

```
172 passed in 15.13s
  - P0 108 (workstream A/B/C + MVP)
  - P1 +31 (adapters, MCP, memory governance)
  - P2 +33 (performance 14 + e2e 19)
```

## 6. Known Limitations

- Host lacks `docker`/`podman`/`kubectl` — container runtime 실기동 미검증 (정적 yaml만 검증).
- Host nginx `ai.openit.co.kr` cert 권한 오류는 기존 호스트 설정이며 `deploy/nginx/nginx.conf` 사이드카와 무관.
- `verify-16a.sh` host checks are expected FAIL until install steps are executed with master approval.
