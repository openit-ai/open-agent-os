# Hermes systemd — 설치 · 검증 절차 (§16A.4~16A.6)

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

## 7. 트러블슈팅

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
