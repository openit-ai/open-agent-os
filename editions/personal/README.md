# Personal Edition — 1인용 에이전트

> 한 사람, 한 대의 머신, 하나의 에이전트. 내 데이터는 내 손 안에, 비용은 월 $10 수준.

[← README로 돌아가기](../../README.md) · [쿡북](../../docs/cookbook.md) · [FAQ](../../docs/faq.md)

## 이런 분께

- 개인 비서가 필요하지만 클라우드 구독이 부담스러운 분
- 메모·문서·결정이 흩어져 "내 두 번째 뇌"가 필요한 분
- 내 데이터가 외부 서비스에 쌓이는 게 불편한 분
- 미니PC나 여분 PC가 한 대 있는 분

## 구성

**필수**

- Hermes Agent (에이전트 런타임)
- LLM 플랜 — 저가 정액 예: OpenCode Go(월 약 $10), 또는 Nous Portal
- Telegram 봇 (무료) — 폰에서 에이전트와 대화하는 창구
- 지식 위키 — git 저장소 기반 세컨드 브레인

**선택**

- Obsidian — 위키를 그래프 뷰로 보기
- 메일 연동 (Himalaya) — 메일 확인·초안
- 예약 작업 — 아침 브리핑, 백업, 감시

**구동 환경:** N100급 미니PC · 16GB RAM 권장 · Ubuntu LTS

## 설치 흐름 — 게이트 3개

```text
G0  에디션 선택          "Personal로 진행할까요?"
G1  LLM 플랜             가입 링크 → 키 붙여넣기 (즉시 검증)
G2  Telegram             @BotFather 봇 토큰 + 내 사용자 ID 붙여넣기
────────────────────────
이후: 설치 → 위키 시딩 → 하네스 시딩 → 검증 12항목 → 보고
```

에이전트가 읽고·설치하고·검증합니다. 사용자는 게이트만 처리합니다.

에이전트는 저장소에서 `bash editions/personal/install.sh`를 실행합니다 (`--dry-run`으로 계획 확인).
최종 확인은 `bash bootstrap/verify/personal-verify.sh`로 실행하며, 결과에는 사람 확인 항목도 구분됩니다.

## 비용

| 항목 | 비용 |
|---|---|
| LLM 플랜 | 월 약 $10 (저가 정액 예시) |
| Telegram | 무료 |
| 하드웨어 | 보유 PC 또는 미니PC 1대 |
| 추가 구독 | 없음 |

## 설치 후 첫걸음

1. "매일 아침 8시에 브리핑해줘"
2. "이거 기억해둬: [중요한 사실]"
3. "이번 주에 뭐 했는지 정리해줘"
4. 더 많은 레시피 → [쿡북](../../docs/cookbook.md)

## 라이선스

**Apache License 2.0** — 오픈소스. 자유롭게 사용·수정·배포할 수 있습니다(상업적 사용 포함). [`LICENSE`](../../LICENSE) 참조.

## 관련 문서

- [부트스트랩 스킬](../../skills/oaos-bootstrap/SKILL.md) — 설치 절차의 정본
- [하네스 구성](../../harness/README.md) — SOUL/USER/MEMORY/AGENTS
- [FAQ](../../docs/faq.md)
