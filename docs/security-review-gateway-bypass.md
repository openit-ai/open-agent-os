# Security Review — Execution Gateway Bypass Risk

> **검토 대상:** 첨부 이미지 평가 — "Execution Gateway는 잘 만들어도 Hermes가 shell/docker/DB/내부망/credential에 직접 접근 가능하면 우회 가능, Gateway 존재보다 우회 불가능이 중요"
> **대상 레포:** `openit-ai/open-agent-os v0.1.1` — `docs/architecture-v1.1.md` (§6, §16, §19–25) + 구현 코드 (control-plane / execution-gateway / security / deploy)
> **결론:** 명제 자체는 정확(80% 타당). O-AOS는 이를 전제로 설계되어 아키텍처 9/10, 프로덕션 강제(네트워크·런타임·DB 재검증)는 잔여 과제.

---

## 1. 첨부 평가 요약

```
Hermes (LLM Agent, 코드 생성·실행 가능)
  ├─ shell
  ├─ docker socket
  ├─ DB 직접 접속
  ├─ 내부 네트워크 / Internal API curl
  ├─ credential 파일
  └─ 임의 Python/script 실행
        │
        └─→ Execution Gateway 우회 → DB/내부 시스템 직접 침투

=> Gateway가 "잘 만들어졌는가"는 중요하지 않다.
   "Gateway를 우회할 수 없는가"가 진짜 보안 경계다.
```

이는 OWASP LLM Top 10, MITRE ATLAS의 LLM Agent 위협 모델과 일치하는 정설이다. `prompt injection → agent compromise → Gateway bypass` 경로는 현실적이다.

---

## 2. Open Agent OS 설계의 방어 — 이미 반영된 부분

| 공격 경로 (이미지) | O-AOS 설계 방어 | 구현 상태 (v0.1.1) |
|---|---|---|
| Hermes → DB 직접 | DB는 Hermes 직접 접근 불가. `Execution Gateway`의 `Privileged Tool Proxy` + `Capability Token (HS256 300s, nonce/jti replay 방지)` 없이는 실행 거부 | `execution-gateway/proxy` + `HIGH token 필수` — 108 tests 검증됨 |
| Hermes → Internal API curl | 권한 판단은 LLM이 아니라 `Policy Engine (fnmatch, Strict, Explicit Deny > Personal)` 코드가 수행. `Agent Permission ≤ User Permission` | `security/policy-engine` 구현됨 |
| 임의 Python 생성·실행 | `Security-Domain Worker Pool` 분리 — General / Dev / Finance·HR / Admin / High-risk ephemeral sandbox (§16). HIGH는 일회성 sandbox | `control-plane/router (HIGH→ephemeral)` 구현, container 격리는 deploy 스캐폴딩 수준 |
| credential 탈취 | Vault는 Hermes 프로세스에 장기 저장 금지, `Fernet AES+HMAC`, owner `agent:assistant:<user>` 격리, plaintext 저장 금지 (§10) | `security/credential-vault (Fernet)` 구현, `EncryptedPostgresVault`는 stub |

---

## 3. 남은 과제 — Gateway를 "우회 불가능"하게 만드는 3가지

이미지 평가가 100% 들어맞는 지점은 배포 레벨 강제다. 설계는 있으나 **프로덕션에서 강제 증명**이 남았다.

### 3.1 NetworkPolicy — Hermes의 직접 경로 차단

- Hermes Pod는 `Control Plane (8000)` / `Execution Gateway (8001)` 외 `DB / 내부 API` 직접 접근을 `Kubernetes NetworkPolicy`로 차단
- 현재 `deploy/k8s`는 스캐폴딩 — 실제 차단 규칙 미검증

### 3.2 Runtime Hardening — Hermes 컨테이너 최소 권한

- `docker.sock` 마운트 제거
- `shell / curl / python` 임의 실행 비활성화
- `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `seccomp / AppArmor`
- 현재 `mock_executor` 기반이라 실제 LLM 코드 실행 경로 미구현 — hardening PoC 필요

### 3.3 DB / Internal API 레벨 재검증 — 방어의 이중화

- Gateway뿐 아니라 DB, Internal API도 `Capability Token`을 재검증
- Gateway가 우회되어도 최종 자원 계층에서 거부되도록
- 현재는 Gateway에서만 검증 — DB/API 재검증 레이어 추가 필요

---

## 4. 판정

| 항목 | 점수 | 근거 |
|---|---|---|
| 아키텍처 설계 | 9/10 | 권한 분리·Token·Vault·ephemeral sandbox — 이미지 위협을 전제로 설계 |
| 프로덕션 강제 | 6/10 | NetworkPolicy / Runtime hardening / DB 재검증 — KVM4 `docker-compose.prod.yml`에서 증명 필요 |
| 첨부 평가 타당성 | 8/10 | "우회 불가능이 중요" 명제는 정확. O-AOS에는 절반 적용, 절반은 잔여 과제 |

> **한 줄 결론:** Gateway를 잘 만드는 것은 1단계, 우회할 수 없게 만드는 것이 2단계 — O-AOS는 1단계를 코드로 끝냈고, 2단계를 배포·런타임에서 증명하면 진짜 보안 경계가 된다.

---

## 5. 권장 Next Step

1. `deploy/k8s`에 NetworkPolicy 추가 — Hermes → DB 직접 차단 E2E 테스트
2. Hermes 컨테이너 hardening 프로파일 적용 — `docker.sock` 제거·readOnly 검증
3. DB/API 레이어 Capability Token 재검증 미들웨어 추가

*작성: 2026-08-27 / 검토: docs/architecture-v1.1.md Sections 6, 16, 19–25*
