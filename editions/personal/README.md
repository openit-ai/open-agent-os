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

**권장 구동 환경:** N100급 미니PC · 16GB RAM · Ubuntu LTS

**지원 OS:** Ubuntu(Linux)·macOS·Windows 11 — Hermes Agent 네이티브 설치가 가능한 운영체제와 동일합니다.

## 설치 흐름 — 게이트 3개

```text
선택  에디션 선택          "Personal로 진행할까요?"
G1  LLM 플랜             가입 링크 → 키 붙여넣기 (즉시 검증)
G2·G3  Telegram          @BotFather 봇 토큰 + 내 사용자 ID 붙여넣기
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

## 옵션 — Hermes 데스크탑 앱으로 쓰기

서버는 그대로 두고 쓰는 PC에 **Hermes 데스크탑 앱**만 설치하면, 같은 에이전트를 그래픽 화면에서 쓸 수 있습니다. 텔레그램과 동시에 연결돼도 되고, 채팅·산출물·봇·설정을 한 창에서 다룹니다. 앱 내려받기: [Hermes 데스크탑](https://hermes-agent.nousresearch.com/desktop) (Windows·macOS·Linux)

**1. 서버 준비 — 대시보드에 로그인을 설정하고 내부망에 열기**

`~/.hermes/.env`에 계정을 설정합니다.

```text
HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=<강한 비밀번호>
HERMES_DASHBOARD_BASIC_AUTH_SECRET=<openssl rand -base64 32 결과>
```

대시보드를 내부망에 열어 상시 구동합니다(Linux 호스트에서는 systemd 서비스 권장).

```text
hermes dashboard --host 0.0.0.0 --port 9119 --no-open
```

> 대시보드를 루프백 밖에 열면 로그인이 강제됩니다(미설정 시 시작 거부) — 내부망 전용 사용을 권장합니다.

**2. 앱에서 연결 — Settings → Gateways → Add connection → Remote gateway**

- Remote URL에 `http://<서버 IP>:9119` 입력 → **Sign in**에서 위 사용자 이름·비밀번호로 로그인
- **Test**로 연결을 확인합니다 — 로그인은 한 번만 하면 세션이 유지됩니다

**3. 인터넷에서도 쓰려면 (선택)**

대시보드를 HTTPS 리버스 프록시(예: `https://<도메인>/hermes`) 뒤에 두고, 같은 방식으로 Remote URL에 그 주소를 입력합니다.

**이렇게 활용합니다**

- **세션** — 어디서 나눈 대화든 이어서: 실시간 스트리밍·검색·Artifacts(산출물 갤러리)
- **봇 탭(봇 모드)** — 전용 봇 로스터·그룹 협업 (데스크탑에 기본 탑재)
- **화면(Screen)** — 서버의 가상 화면(컴퓨터 사용)을 열어 보고, 필요하면 사람이 직접 이어받기
- **설정·운영** — Config·API 키·스킬·예약 작업·채널을 GUI에서 관리

이 준비도 에이전트에게 그대로 요청하면 됩니다 — "데스크탑 앱에서도 쓸 수 있게 열어줘".

자세히: [데스크탑 가이드](https://hermes-agent.nousresearch.com/docs/user-guide/desktop) · [원격 백엔드 연결](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard) · [멀티 연결 가이드](https://hermes-agent.nousresearch.com/docs/user-guide/multi-connection-desktop)

## 라이선스

**Apache License 2.0** — 오픈소스. 자유롭게 사용·수정·배포할 수 있습니다(상업적 사용 포함). [`LICENSE`](../../LICENSE) 참조.

## 관련 문서

- [부트스트랩 스킬](../../skills/oaos-bootstrap/SKILL.md) — 설치 절차의 정본
- [하네스 구성](../../harness/README.md) — SOUL/USER/MEMORY/AGENTS
- [FAQ](../../docs/faq.md)
