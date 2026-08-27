# K8s manifests — customer-owned cluster (Private K8s / On-Prem)

Quick ref — full procedure in `deploy/README.md` §3.

```bash
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/configmap.yaml
# secrets: kubectl create secret generic oaos-secrets --from-literal=... -n open-agent-os
# tls:     kubectl create secret tls oaos-tls --cert=tls.crt --key=tls.key -n open-agent-os
kubectl apply -f deploy/k8s/postgres-statefulset.yaml
kubectl apply -f deploy/k8s/redis-deployment.yaml
kubectl apply -f deploy/k8s/control-plane/
kubectl apply -f deploy/k8s/execution-gateway/
kubectl apply -f deploy/k8s/security/
kubectl apply -f deploy/k8s/ingress.yaml
```

Manifests:

- `namespace.yaml` — Namespace `open-agent-os`
- `configmap.yaml` — `oaos-config` (non-secret env)
- `secret.yaml.template` — `oaos-secrets` template (never apply as-is)
- `postgres-statefulset.yaml` — StatefulSet + headless Service
- `redis-deployment.yaml` — Deployment + Service + PVC
- `control-plane/deployment.yaml` + `service.yaml`
- `execution-gateway/deployment.yaml` + `service.yaml`
- `security/deployment.yaml` + `service.yaml`
- `ingress.yaml` — nginx Ingress with TLS (host: `open-agent-os.example.com`)

> Host `open-agent-os.example.com` → replace with customer domain. `storageClassName: standard` → override per cloud.
