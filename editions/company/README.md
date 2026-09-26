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

## 기반: 기존 플랫폼 흡수

Company 에디션은 별도 개발·제공되는 엔터프라이즈 플랫폼(개인 에이전트·Personal Wiki·지식 인덱스·정책·승인·감사·Vault·관리 콘솔)을 기반으로 합니다. v2.0 설계에서 이 자산들을 Company 레이어로 흡수합니다.

- [설계서](../../docs/architecture-v2.0.md) — 3-에디션 설계(정본)

## 상태

Company 통합은 **P2 로드맵**입니다 — 기존 플랫폼 흡수 + 선택 연동 + 멀티 LLM 라우팅이 순차 반영됩니다. 현재는 Personal → Project 순서로 구축하며, Company는 같은 코어 위에 거버넌스를 얹는 구조입니다.

## 라이선스

**Business Source License 1.1 (BSL 1.1)** — 소스 공개. 평가·개발·테스트 사용은 허용되며, 프로덕션·상업적 사용은 별도 상업 라이선스가 필요합니다. Change Date(2030-08-27) 이후 Apache License 2.0으로 전환됩니다. [`LICENSE-COMPANY`](../../LICENSE-COMPANY) 참조.

## 관련 문서

- [부트스트랩 스킬](../../skills/oaos-bootstrap/SKILL.md) · [게이트 레퍼런스](../../skills/oaos-bootstrap/references/gates.md)
- [FAQ](../../docs/faq.md) · [설계서](../../docs/architecture-v2.0.md)
