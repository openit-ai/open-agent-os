# OAOS systemd production + Hermes — 설치 · 검증 (§5, §16A)

> **두 배포판 병렬 관리:** Docker (`deploy/docker-compose.*.yml` + `.env`) 와
> systemd (`config/oaos.env.example` / `deploy/systemd/oaos.env.example` + `deploy/systemd/*.service`) 는 **별도 경로, 동일 코드**입니다. 공통 코드 변경 없이 배포별 설정만 분리합니다. [`deploy/README.md`](../README.md) §5 참조.

---

## A. OAOS systemd — Control Plane (production, `oaos-control-plane :8100`)

현재 운영 서버 `192.168.6.61` 에서 동작 중인 유닛과 **동일한 구성**을 재현합니다:

- `oaos-control-plane.service` (user): `~/.config/systemd/user/oaos-control-plane.service` — `WorkingDirectory /home/openitsvc/open-agent-os/control-plane`, `EnvironmentFile control-plane/.env`, `ExecStart …/venv/bin/python -m uvicorn control_plane.app:app --host 127.0.0.1 --port 8100`
- `oaos-admin-api.service` (system, root): `/etc/systemd/system/oaos-admin-api.service` — `:8010`, `admin-console/backend`
- `oaos-admin-console.service` (system, root): `:3012` (Next.js)
- 본 패키지는 **Control Plane `:8100`** 을 systemd 로 재설치하는 경로를 제공하며, `execution-gateway :8001` / `security :8002` 는 entrypoint 검증 후 `--with-optional` 로만 설치합니다.

### A.1 파일 위치

| 파일 | 용도 | 비고 |
|---|---|---|
| `config/oaos.env.example` | **systemd 전용** unified env 템플릿 (canonical) | Docker의 `.env.example` 과 별도 |
| `deploy/systemd/oaos.env.example` | 동일 템플릿의 mirror (deploy 내부 참조용) | `config/oaos.env.example` 와 동일 내용 |
| `scripts/check-production-config.sh` | 친절한 프리플라이트 (no secret output) | `deploy/systemd/check-production-config.sh` 는 wrapper |
| `deploy/systemd/install-systemd.sh` | systemd 유닛 설치 (no credential invention) | `--user` / `--system` 지원 |
| `deploy/systemd/oaos-control-plane.service` | system unit `:8100` (template) | 설치 시 `REPO_ROOT`/`PYTHON_BIN` 자동 패치 |
| `deploy/systemd/user/oaos-control-plane.service` | user unit `:8100` (mirrors production user unit) | `systemctl --user` 전용 |
| `deploy/systemd/oaos-execution-gateway.service` | optional `:8001` (verified `execution_gateway.app:app`) | `--with-optional` 로만 설치 |
| `deploy/systemd/oaos-security.service` | optional `:8002` (verified `app:app`) | `--with-optional` 로만 설치 |

### A.2 빠른 시작

```bash
# 1) env 준비 — 템플릿 복사 → 0600 → CHANGE_ME_* 모두 교체 (절대 커밋 금지)
cp config/oaos.env.example config/oaos.env
chmod 600 config/oaos.env
vi config/oaos.env
# 필요 값:
#   DATABASE_URL, REDIS_URL, JWT_SIGNING_KEY(≥32), AUDIT_SIGNING_KEY(≥32),
#   ADMIN_JWT_SECRET(≥32), VAULT_ENCRYPTION_KEY|OAOS_ENCRYPTION_KEY(≥32), OAOS_ENV=production
# 생성: openssl rand -base64 32

# 2) 프리플라이트 — 누락 변수 + 파일 위치를 명확히 안내, secret 미출력
bash scripts/check-production-config.sh --env-file config/oaos.env --verbose
# 또는 wrapper:
bash deploy/systemd/check-production-config.sh --env-file config/oaos.env

# 실패 예시 (secret은 길이만 표시):
#   [ERROR] Missing or placeholder: JWT_SIGNING_KEY — file: config/oaos.env (len=8)
#     → Fix: set JWT_SIGNING_KEY in config/oaos.env (≥32 chars, not CHANGE_ME)
#        Generate: openssl rand -base64 32
#   [ERROR] OAOS_ENV must be 'production' — file: config/oaos.env (found: 'development')

# 3a) user 설치 (sudo 불필요 — 현재 운영 192.168.6.61 user unit과 동일)
bash deploy/systemd/install-systemd.sh --user --env-file config/oaos.env --dry-run
bash deploy/systemd/install-systemd.sh --user --env-file config/oaos.env
systemctl --user daemon-reload
systemctl --user status oaos-control-plane.service
journalctl --user -u oaos-control-plane -f
curl -s http://127.0.0.1:8100/healthz | jq
curl -s http://127.0.0.1:8100/readyz | jq

# 3b) system 설치 (sudo 필요, /etc/oaos/oaos.env)
sudo mkdir -p /etc/oaos
sudo cp config/oaos.env.example /etc/oaos/oaos.env
sudo chmod 600 /etc/oaos/oaos.env && sudo vi /etc/oaos/oaos.env
bash scripts/check-production-config.sh --env-file /etc/oaos/oaos.env
sudo bash deploy/systemd/install-systemd.sh --env-file /etc/oaos/oaos.env --dry-run
sudo bash deploy/systemd/install-systemd.sh --env-file /etc/oaos/oaos.env
systemctl status oaos-control-plane.service
curl -s http://127.0.0.1:8100/healthz | jq
# optional: gateway+security까지 (entrypoint 검증 후)
sudo bash deploy/systemd/install-systemd.sh --env-file /etc/oaos/oaos.env --with-optional
systemctl status oaos-execution-gateway.service oaos-security.service

# 4) 검증 (공통)
curl -s http://127.0.0.1:8100/healthz          # liveness — always 200
curl -s http://127.0.0.1:8100/readyz           # readiness — prod 503 on degraded, draining 503
curl -s http://127.0.0.1:8100/v1/health/detailed | jq
systemd-analyze verify /etc/systemd/system/oaos-control-plane.service
# user:
systemd-analyze verify ~/.config/systemd/user/oaos-control-plane.service
```

### A.3 `check-production-config.sh` 동작

- 검사 항목: `DATABASE_URL`, `JWT_SIGNING_KEY(≥32)`, `AUDIT_SIGNING_KEY(≥32)`, `ADMIN_JWT_SECRET(≥32)`, `VAULT_ENCRYPTION_KEY|OAOS_ENCRYPTION_KEY(≥32)`, `OAOS_ENV=production`
- placeholder 거부: `CHANGE_ME*`, `secret`, `dev-*`, 빈 값, 길이 부족
- 출력: 변수명 + 파일 경로 + `len=N` 만 표시, **secret 값 절대 미출력** (fail-closed 보안 유지)
- 종료 코드: `0=OK`, `1=missing/weak`, `2=file not found`
- 자동 탐색: `--env-file` 없을 시 `OAOS_ENV_FILE` → `/etc/oaos/oaos.env` → `config/oaos.env` → `control-plane/.env` → `.env` 순

### A.4 `install-systemd.sh` 동작

- **Never invent credentials:** 프리플라이트 실패 시 즉시 abort, remediation만 안내 (secret 미출력)
- `REPO_ROOT` 자동 감지 → unit의 `WorkingDirectory`, `PYTHONPATH`, `ReadWritePaths` 패치
- `PYTHON_BIN` 자동 감지: `$OAOS_PYTHON` → `~/.hermes/hermes-agent/venv/bin/python` → `/usr/bin/python3.12` → `/usr/bin/python3` (fastapi import 검증)
- `--user` → `~/.config/systemd/user/oaos-control-plane.service` (no sudo), `--system` → `/etc/systemd/system/oaos-control-plane.service`
- `--with-optional` → `oaos-execution-gateway.service`/`oaos-security.service` 는 **entrypoint 검증(`PYTHONPATH` import) 성공 시에만** 설치
- `--dry-run` → 복사/패치/daemon-reload 를 로그만 출력

### A.5 운영 대조표 (192.168.6.61)

| 운영 유닛 (현재) | 본 패키지의 대응 | 포트 | EnvironmentFile |
|---|---|---|---|
| `oaos-control-plane.service` (user, `openitsvc`, control-plane/.env, hermes venv python) | `deploy/systemd/user/oaos-control-plane.service` | 8100 | `config/oaos.env` or `control-plane/.env` |
| `oaos-admin-api.service` (root, admin-console/backend, 8010) | (기존 운영 유지 — 본 패키지 범위 외) | 8010 | — |
| `oaos-admin-console.service` (root, Next.js 3012) | — | 3012 | — |
| (systemd 신규) | `deploy/systemd/oaos-control-plane.service` | 8100 | `/etc/oaos/oaos.env` |

### A.6 트러블슈팅 (OAOS)

| 증상 | 조치 |
|---|---|
| `check-production-config.sh: [ERROR] Missing JWT_SIGNING_KEY — file: ...` | 해당 파일에서 `JWT_SIGNING_KEY` 를 `openssl rand -base64 32` 값으로 교체, `chmod 600` 유지 |
| `install-systemd.sh: Preflight failed — abort` | 프리플라이트 에러를 먼저 수정. Installer는 절대 자격증명을 자동 생성하지 않음 |
| `systemctl: Failed to start oaos-control-plane.service: ExecStart not found` | `PYTHON_BIN` 경로 확인 — `which python3` / `ls ~/.hermes/hermes-agent/venv/bin/python`, `OAOS_PYTHON` 로 오버라이드 |
| `Permission denied` / `Read-only file system` | unit의 `ProtectSystem`/`ReadWritePaths` 확인 — repo가 `/home/openitsvc/open-agent-os` 외에 있으면 `ReadWritePaths` 패치 필요 |
| `curl: Connection refused :8100` | `systemctl [--user] status`, `journalctl -u oaos-control-plane -n 50`, `curl /healthz` 로 로그 확인 |
| `Port already in use` | Docker와 systemd를 동시 실행했는지 확인 — 둘 중 하나만 선택 |

---

## B. Hermes systemd — 설치 · 검증 절차 (§16A.4~16A.6)

> Spec: `docs/architecture-v1.3.md` §16A.4 (전용 OS 계정), §16A.5 (Filesystem Isolation), §16A.6 (Network Isolation)

## 1. 사전 요구사항

- RHEL / Rocky Linux 9+ 또는 Ubuntu 22.04+
- systemd 250+
- nftables 또는 firewalld 중 하나
- root / sudo 권한 (설치 시에만)

## 2. hermes OS 계정 생성 (§16A.4)

```bash
sudo bash deploy/scripts/create-hermes-user.sh

# 검증
id hermes
ls -ld /home/hermes          # 0750 hermes:hermes
sudo -l -U hermes            # hermes ALL=(ALL) !ALL
sudo -u hermes id
```

- `HOME=/home/hermes`, `No sudo`, `passwd -l` 잠금, `/etc/sudoers.d/99-hermes-deny` (0440) 적용됨
- 재실행 시 idempotent — 기존 계정이 있으면 속성만 보정

## 3. 바이너리 / 환경 준비

```bash
# 예: Hermes 바이너리 설치 (경로에 맞게 수정)
sudo install -m 0755 ./hermes /usr/local/bin/hermes
sudo chown root:root /usr/local/bin/hermes

# hermes 전용 env (선택, 0600 — 기업 secret 절대 금지)
sudo -u hermes mkdir -p /home/hermes/.hermes
sudo -u hermes chmod 0700 /home/hermes/.hermes
# echo "HERMES_ACP_URL=http://10.10.1.10:8000" | sudo -u hermes tee /home/hermes/.hermes/env
```

> **주의:** DB_PASSWORD, ERP_API_KEY, GOOGLE_REFRESH_TOKEN 등 기업 credential을
> `Environment=` / `EnvironmentFile=` 로 주입하지 않는다 (§16A.7).
> Hermes는 short-lived capability만 보유하고 실제 credential은 MCP/Execution Gateway + Vault가 조회한다.

`hermes.service` 의 `ExecStart` 를 실제 진입점에 맞게 수정:

```ini
ExecStart=/usr/local/bin/hermes --config /home/hermes/.hermes/config.yaml
```

## 4. systemd 유닛 설치 (§16A.5)

```bash
sudo cp deploy/systemd/hermes.service /etc/systemd/system/hermes.service
sudo systemd-analyze verify /etc/systemd/system/hermes.service
sudo systemctl daemon-reload
sudo systemctl enable --now hermes.service
```

### Filesystem Isolation 검증

```bash
systemctl show hermes.service -p User -p NoNewPrivileges -p PrivateTmp \
  -p ProtectSystem -p ProtectHome -p ReadWritePaths

# 예상:
# User=hermes
# NoNewPrivileges=yes
# PrivateTmp=yes
# ProtectSystem=strict
# ProtectHome=yes
# ReadWritePaths=/home/hermes

# 샌드박스 동작 확인
sudo -u hermes bash -c 'touch /home/hermes/test.ok && echo "RW /home/hermes OK"'
sudo -u hermes bash -c 'touch /root/test 2>&1 | head'        # DENY
sudo -u hermes bash -c 'ls /home/openitsvc 2>&1 | head'      # DENY (ProtectHome)
sudo -u hermes bash -c 'touch /etc/test 2>&1 | head'         # DENY (ProtectSystem=strict)

# systemd가 적용한 mount namespace 확인
sudo systemctl status hermes.service
```

추가로 `InaccessiblePaths=/root` 등이 이미 적용됨. 고객 환경에서
Vault/DB 데이터 경로가 별도이면 `InaccessiblePaths=` 를 추가한다.

## 5. Network Isolation (§16A.6)

### nftables (권장)

```bash
# 변수 편집: deploy/firewall/hermes-egress.nft 상단의 define ACP_HOST/MCP_HOST/LLM_HOSTS 등
vi deploy/firewall/hermes-egress.nft

sudo mkdir -p /etc/nftables
sudo cp deploy/firewall/hermes-egress.nft /etc/nftables/hermes-egress.nft
sudo nft -c -f /etc/nftables/hermes-egress.nft   # syntax check
sudo nft -f /etc/nftables/hermes-egress.nft
sudo nft list ruleset | grep -A2 hermes

# 영구 적용 (distro별)
# Rocky/RHEL: /etc/sysconfig/nftables.conf 에 include 추가 또는
#   sudo systemctl enable --now nftables
# Ubuntu: /etc/nftables.conf 로 복사
```

### firewalld 대안

`deploy/firewall/firewalld-alternative.md` 참조 — `firewall-cmd --direct` 규칙으로
동일한 ALLOW(ACP/MCP/LLM) / DENY(DB/ERP/CRM) 정책을 적용한다.

### Network 검증

```bash
# hermes 유저로 수행 — 성공 케이스
sudo -u hermes curl -v --connect-timeout 5 http://10.10.1.10:8000/health  # ACP ALLOW
sudo -u hermes curl -v --connect-timeout 5 http://10.10.1.11:8001/health  # MCP ALLOW
sudo -u hermes getent hosts api.openai.com && sudo -u hermes curl -I https://api.openai.com  # LLM ALLOW (443)

# hermes 유저로 수행 — 차단 케이스 (DROP/timeout 이어야 정상)
sudo -u hermes curl -v --connect-timeout 3 http://10.20.0.5:5432/    # DB DENY
sudo -u hermes nc -vz --wait 3 10.30.0.5 443                         # ERP DENY
sudo -u hermes nc -vz --wait 3 10.40.0.5 443                         # CRM DENY
sudo -u hermes ssh -o ConnectTimeout=3 10.0.0.5 2>&1 | head           # SSH DENY
```

## 6. 전체 상태 점검

```bash
systemctl is-active hermes.service
systemctl is-enabled hermes.service
sudo nft list ruleset | grep hermes  # 또는 firewall-cmd --direct --get-all-rules | grep hermes
ls -R ~/open-agent-os/deploy
```

## 7. 트러블슈팅 (Hermes)

| 증상 | 조치 |
|---|---|
| `Failed to start hermes.service: ExecStart not found` | `ExecStart` 경로 수정 후 `daemon-reload` |
| `Read-only file system` (로그) | Hermes가 `/home/hermes` 밖 쓰기 시도 — 코드/설정에서 HOME 외부 접근 제거 |
| `curl: Could not resolve host` (hermes) | DNS 53 허용 확인 (`udp/tcp dport 53` 규칙) |
| LLM 호출 차단 | `LLM_HOSTS` / 443 ALLOW 범위 확인, HTTP proxy 사용 시 proxy 주소 ALLOW |
| `visudo: syntax error` (user 생성 시) | `/etc/sudoers.d/99-hermes-deny` 수동 삭제 후 재실행 |

## 8. 관련 문서

- `docs/architecture-v1.3.md` §16A.1~§16A.9
- `deploy/firewall/hermes-egress.nft`
- `deploy/firewall/firewalld-alternative.md`
- `deploy/scripts/create-hermes-user.sh`
- OAOS systemd: `config/oaos.env.example`, `scripts/check-production-config.sh`, `deploy/systemd/install-systemd.sh`
