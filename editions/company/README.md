# Company Edition — 5~50인 기업용 에이전트 OS

> 구성원마다 개인 에이전트를, 회사에는 거버넌스를. 중소기업(5~50인)을 위한 자체 운영형 AI 업무 기반.

[← README로 돌아가기](../../README.md) · [쿡북](../../docs/cookbook.md) · [FAQ](../../docs/faq.md)

## 이런 분께

- 구성원 전체에 AI 비서를 주되, 데이터·권한·감사는 회사가 통제하고 싶은 기업
- 사내 지식이 권한을 지키며 검색되는 시스템이 필요한 기업
- 외부 SaaS에 회사 데이터를 맡기지 않고 자체 운영하려는 기업

## 구성

**Project 기반 + 거버넌스 레이어**

- 구성원별 개인 에이전트 — 각자 하네스(SOUL/USER/MEMORY)와 지식
- 정책 + 승인 — 허용 도구·행동 범위, 파괴적 작업은 관리자 JIT 승인
- 감사 원장 — 누가·언제·무엇을 했는지 기록
- Secret Vault — 시크릿 중앙 관리
- 권한 인식 지식 인덱스 — ACL을 지키는 사내 검색(RAG)
- 관리 콘솔 — 구성원·정책·상태 관리
- 멀티 LLM 라우팅 — 민감·고난도는 상위 모델, 일상은 저가 모델

**선택 연동** (이미 쓰는 서비스에 연결 — 추가 구독 아님)

- Slack · Notion · Google Workspace · Microsoft 365

## 설치 흐름 — 필수 게이트 9개 + 선택

```text
선택  에디션 선택          "Company로 진행할까요?"
G1  LLM 플랜             가입 링크 → 키 붙여넣기
G2  Telegram 봇 토큰     @BotFather 토큰 붙여넣기
G3  Telegram 사용자 ID   숫자 ID 붙여넣기
G0·G6  VPS + 도메인      구매 → IP·도메인 전달
G7  Mattermost 관리자    초기 관리자 계정 생성
G8  Outline API 토큰     관리자 가입 → API 토큰 붙여넣기
G9  봇 메일함            전용 메일 계정 → 앱 비밀번호
G10 관리 콘솔            초기 관리자·구성원 매핑
G11·G12 (선택) 연동      Workspace/365 또는 Slack/Notion
G13 (선택) 멀티 LLM     추가 모델 API 키
────────────────────────
이후: 설치 → 거버넌스 셋업 → 구성원 등록 → 검증 → 보고
```

## 비용

| 항목 | 비용 |
|---|---|
| Project 비용 일체 | LLM 월 약 $10 + VPS + 도메인 |
| 거버넌스 레이어 | 오픈소스 기반 — 동일 서버에서 시작 가능 |
| Slack·Notion·Workspace·365 | **기존 지출** — 연동만, 추가 비용 아님 |
| 추가 구독 | 없음 |

## 기반: Project 운영 패턴·기존 자산 선별 흡수

Company 에디션은 Project의 단일 서버·systemd 운영 패턴을 승계합니다. 별도 플랫폼의 개인 에이전트·개인 위키·지식 인덱스·정책·승인·감사·Vault·관리 콘솔은 공개·이식 적합성을 확인해 필요한 부분만 Company 레이어로 선별 흡수합니다. Docker·Kubernetes 배포 경로는 흡수하지 않습니다.

- [설계서](../../docs/architecture-v2.0.md) — 3-에디션 설계(정본)

## C02: E 검증 인스턴스 단일 서버 배포

C02는 [Company 설계 §3·§9](../../docs/company-design-v1.0.md)를 따릅니다. C01을 E에서 `verified`로 확인한 다음, **기존 Project 게이트웨이와 다른 비권한 Company 계정**으로 실행합니다. Ubuntu LTS, 실행 가능한 `systemctl --user`, 기존 TLS nginx vhost와 certbot 갱신 타이머, 분리된 E PostgreSQL, 설치 전 DB 백업과 원복 디렉터리가 필요합니다. 새 서버나 구독이 필요하면 승인 전 설치를 중단합니다.

다음 값은 E 운영자가 환경에 설정합니다. 경로는 미리 생성하고 Company 계정에 쓰기 권한을 부여합니다. 모든 백업·원복 디렉터리는 `0700`이어야 합니다. 비밀값은 명령 인수나 이 환경변수 목록에 넣지 않습니다.

```bash
export OAOS_COMPANY_USER="$(id -un)" OAOS_COMPANY_E_ID=E-example
export PGDATABASE=company_e PGHOST=127.0.0.1
export OAOS_COMPANY_HTTPS_ORIGIN=https://e.example.test
export OAOS_COMPANY_NGINX_VHOST=/etc/nginx/sites-available/oaos-portal.conf
export OAOS_COMPANY_BACKUP_DIR=/srv/oaos-company-e/backups
export OAOS_COMPANY_KEY_BACKUP_DIR=/srv/oaos-company-e/key-backups
export OAOS_COMPANY_ROLLBACK_DIR=/srv/oaos-company-e/rollback
bash editions/company/install.sh --stage c02 --dry-run
bash editions/company/install.sh --stage c02
bash editions/company/install.sh --status
```

설치기는 매 실행 전 E DB의 일관 덤프와 기존 nginx vhost를 원복 디렉터리에 보존합니다. `.env`는 Company 계정의 `~/.config/oaos-company`에 `0700` 디렉터리·`0600` 파일로 생성됩니다. 재부팅 후 검증에 필요한 비밀 아닌 E 설정값은 같은 디렉터리의 `deployment.conf`에 `0600`으로 보관합니다. 이후 Phase의 자격과 마스터 키는 `.env`에만 안전하게 등록합니다. C02 자체는 비밀값을 생성하지 않습니다. `oaos-company.service`는 `127.0.0.1:8765`의 `/company/health`만 응답하는 표준 라이브러리 기반 임시 프로세스이며, 업무 API·인증·OAuth 기능은 후속 Phase의 구현 대상입니다. 백업 타이머는 매일 DB 덤프와 Company 상태를 저장하고 `.env` 사본은 별도 제한 디렉터리에 둡니다.

원복이 필요하면 해당 실행의 `rollback/c02-날짜.난수` 스냅샷에서 nginx vhost와 기존 유닛·앱 파일을 복원하고 `nginx -t` 뒤 reload합니다. DB 덤프는 E의 분리된 복구 대상에만 복원하며, 자동 역마이그레이션이나 운영 Project DB 덮어쓰기는 하지 않습니다. 키 백업의 별도 보관과 실제 복구 시험은 후속 C17의 완료 조건입니다.

nginx는 지정한 **기존 HTTPS server block**에 `/company/`와 `/company/oauth/callback`만 추가합니다. `nginx -t` 또는 reload가 실패하면 설치 전 vhost를 복원합니다. 인증서 만료와 갱신 타이머, linger 상태를 read-back합니다. DB·Redis·Company 포트가 외부 주소에 바인딩되어 있으면 차단합니다. 외부 방화벽 허용 범위는 E 운영자가 기존 80/443 설정과 함께 확인해야 합니다.

```bash
bash bootstrap/verify/company-verify.sh --phase c02 --read-back
# E 운영자가 백업과 복구 경로를 확인하고 안전하게 재부팅한 뒤 다시 실행
bash bootstrap/verify/company-verify.sh --phase c02 --read-back
```

첫 명령의 성공적인 설치 종료 코드 `0`은 `applied`만 뜻합니다. 검증기는 서비스·백업 타이머의 enabled/active, loopback·HTTPS health, TLS, 재실행 안정성 및 설치 당시와 다른 boot ID에서 재부팅 후 서비스 복귀를 확인한 경우에만 `verified`로 올립니다. 같은 부팅에서 확인하면 종료 코드 `3`과 `applied`를 유지합니다. 게이트 차단은 `3`, 검사 실패는 `1`입니다. 실제 E 설치와 재부팅 검증은 운영자가 수행합니다.

## 상태

Company 통합은 **P2 로드맵**입니다 — 필요한 자산 선별 흡수 + 선택 연동 + 멀티 LLM 라우팅이 순차 반영됩니다. 현재는 Personal → Project 순서로 구축하며, Company는 같은 코어 위에 거버넌스를 얹는 구조입니다.

## 라이선스

**Business Source License 1.1 (BSL 1.1)** — 소스 공개. 평가·개발·테스트 사용은 허용되며, 프로덕션·상업적 사용은 별도 상업 라이선스가 필요합니다. Change Date(2030-08-27) 이후 Apache License 2.0으로 전환됩니다. [`LICENSE-COMPANY`](../../LICENSE-COMPANY) 참조.

## 관련 문서

- [부트스트랩 스킬](../../skills/oaos-bootstrap/SKILL.md) · [게이트 레퍼런스](../../skills/oaos-bootstrap/references/gates.md)
- [FAQ](../../docs/faq.md) · [설계서](../../docs/architecture-v2.0.md)
