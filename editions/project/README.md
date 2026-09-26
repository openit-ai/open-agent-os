# Project Edition — 팀용 에이전트

> 작은 팀(2~6인)을 위한 팀 어시스턴트. 팀 채팅·지식·메일을 하나의 에이전트가 잇습니다.

[← README로 돌아가기](../../README.md) · [쿡북](../../docs/cookbook.md) · [FAQ](../../docs/faq.md)

## 이런 분께

- 팀 채팅·문서·메일이 흩어져 맥락을 다시 모으는 데 시간을 쓰는 팀
- 팀 지식이 사람이 그만둘 때 같이 사라지는 게 아쉬운 팀
- 외주 SaaS가 아니라 직접 통제하는 팀 비서가 필요한 팀

## 구성

**Personal 기반 + 추가**

- VPS 1대 — 4 vCPU / 16GB / 200GB (예: 프로모션 기준 월 약 ₩16,000대, 2026-09 확인)
- Mattermost — 팀 채팅 창구 (+ Telegram 병행)
- Outline — 팀 문서·지식베이스
- 봇 전용 메일함 — 팀 메일 확인·초안·전달
- 팀 계정 — 멤버별 접근 통제(허용목록/페어링)
- 도메인 + HTTPS (nginx + TLS)

## 설치 흐름 — 필수 게이트 8개

```text
선택  에디션 선택          "Project로 진행할까요?"
G1  LLM 플랜             가입 링크 → 키 붙여넣기
G2  Telegram 봇 토큰     @BotFather 토큰 붙여넣기
G3  Telegram 사용자 ID   숫자 ID 붙여넣기
G0·G6  VPS + 도메인      구매 → IP·도메인 전달 (서버 셋업은 에이전트가)
G9  봇 메일함            전용 메일 계정 생성 → 앱 비밀번호 붙여넣기
G7  Mattermost 관리자    초기 관리자 계정 생성
G8  Outline API 토큰     관리자 가입 → API 토큰 붙여넣기
────────────────────────
이후: 설치 → 팀 서비스 셋업 → 멤버 등록 → 검증 → 보고
```

에이전트가 읽고·설치하고·검증합니다. 사용자는 게이트만 처리합니다.

에이전트는 Project VPS에서 `bash editions/project/install.sh --dry-run`으로 계획을 확인한 뒤 `bash editions/project/install.sh`를 실행합니다. 상태는 `~/.oaos-install/state.json`에 저장되며, 미완료 스테이지만 다시 실행할 수 있습니다. 도메인에는 `OAOS_BASE_DOMAIN`, 인증서 이메일에는 `OAOS_ACME_EMAIL`을 전달합니다. 설치 후 `bash bootstrap/verify/project-verify.sh`로 확인합니다.

스테이지 순서는 `prep hermes llm telegram wiki harness cron stack ingress mail team gateway verify`입니다. `team`에서 Mattermost·Outline 자격 증명을 저장한 뒤 처음으로 게이트웨이를 기동합니다. Project 크론은 일일 백업만 등록합니다.
서버 준비 때 SSH 키 로그인이 되는지와 UFW가 활성화되어 있는지를 별도로 확인합니다. 설치기는 기존 UFW 규칙에 필요한 포트만 추가하며 SSH 키 정책과 자동 보안 업데이트는 설정하지 않습니다.

Personal에서 이전하려면 Project 서버에서 `bash bootstrap/migrate/personal-to-project.sh --source user@host --dry-run`으로 계획을 확인하고, 같은 명령을 `--dry-run` 없이 실행합니다. 시크릿 `.env`는 이전되지 않습니다.

설치 게이트에서 에이전트에게 전달할 환경값:

| 게이트 | 값 | 설명 |
|---|---|---|
| G6 | `OAOS_BASE_DOMAIN`, `OAOS_ACME_EMAIL` | 세 A 레코드가 서버 공인 IPv4를 가리켜야 합니다. 다른 도메인을 쓴다면 `OAOS_CHAT_DOMAIN`, `OAOS_NOTE_DOMAIN`, `OAOS_PORTAL_DOMAIN`을 각각 지정합니다. |
| G7 | `MATTERMOST_TOKEN` | `mmctl` 자동 생성이 불가능할 때만 Mattermost 관리 화면에서 발급해 `~/oaos/stack/.env`(600)에 보관합니다. |
| G8 | `OAOS_OUTLINE_API_TOKEN` | Outline 관리자가 Settings → API Keys에서 발급합니다. 에이전트의 Outline API 접근을 위해 Hermes `.env`에 저장합니다. |
| G9 | `OAOS_MAIL_ADDRESS`, `OAOS_MAIL_PASSWORD`, `OAOS_MAIL_IMAP_HOST`, `OAOS_MAIL_SMTP_HOST` | 봇 전용 메일함 값입니다. 비밀번호는 공백을 제거한 앱 비밀번호로 전달합니다. |

`--stage`로 막힌 게이트부터 재시도합니다. 실제 송수신과 허용·비허용 팀 계정 동작은 에이전트와 함께 확인해야 합니다. 신규 VPS 실측 전에는 전체 설치 완료로 보고하지 않습니다.

이미 게이트웨이가 실행 중일 때 `team` 구성을 다시 적용한 경우에는 관리자(사용자)가 별도 셸에서 게이트웨이 서비스를 재시작해야 반영됩니다.

## 비용

| 항목 | 비용 |
|---|---|
| LLM 플랜 | 월 약 $10 수준 (개인과 동일) |
| VPS | 월 약 ₩16,000대~ (스펙·기간에 따라 상이) |
| 도메인 | 연 ₩1~2만 수준 |
| Mattermost·Outline | 셀프 호스팅 (각 서비스 라이선스 확인) |
| 추가 구독 | 없음 |

## 설치 후 첫걸음

1. 팀 채널에 에이전트 초대 → "이번 주 일정 정리해줘"
2. "회의록 정리해서 위키에 넣어줘"
3. "주간 리포트 만들어줘"
4. 더 많은 레시피 → [쿡북](../../docs/cookbook.md)

## 상태

Project 설치·검증·Personal 이전 스크립트가 제공됩니다. 신규 VPS에서 전체 게이트와 실제 서비스 동작을 검증해야 P1 완료로 판정합니다. Mattermost 관리자 생성(G7), Outline 관리자·API 토큰(G8), 봇 메일함(G9)은 사용자 게이트입니다.

## 라이선스

**Apache License 2.0** — 오픈소스. 자유롭게 사용·수정·배포할 수 있습니다(상업적 사용 포함). [`LICENSE`](../../LICENSE) 참조.

## 관련 문서

- [부트스트랩 스킬](../../skills/oaos-bootstrap/SKILL.md) · [게이트 레퍼런스](../../skills/oaos-bootstrap/references/gates.md)
- [FAQ](../../docs/faq.md) · [설계서](../../docs/architecture-v2.0.md)
