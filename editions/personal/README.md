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
- 지식 위키 — git 저장소 기반 세컨드 브레인(`~/data/wiki`) + 로컬 bare 중계(`~/data/wiki.git`)

**선택**

- Obsidian — 위키를 그래프 뷰로 보기
- 메일 연동 (G5, Himalaya + Gmail 앱 비밀번호) — 메일 확인·초안. 설치 스크립트의 선택 스테이지 `mail`이 처리합니다(아래 "G5 — 개인 메일(선택)")
- 예약 작업 추가 — 아침 브리핑 등 (일일 백업과 게이트웨이 워치독은 설치 스크립트가 기본 등록)

**기존 Ubuntu 구동 예시:** N100급 미니PC · 16GB RAM · Ubuntu LTS. macOS·Windows의 자원·절전 조건은 실기기 확인이 필요합니다.

**Personal 설치 스크립트 대상:** Ubuntu(Linux)·Apple Silicon macOS·Windows 11 Git Bash. 3개 레인 구현 및 Linux 스모크/macOS·Windows 모의 검증 완료; CI 3-러너 매트릭스 통과(2026-09-27). 실기기 설치·로그인·백업 검증은 아직 대기 중입니다. Project·Company는 Ubuntu LTS 서버 경로입니다.

## 설치 흐름 — 선택 게이트 2개 + 값 게이트 3개(G1~G3) + 선택 게이트 G5

```text
선택  에디션 선택           "Personal로 진행할까요?"
선택  Hermes 설치 승인      내려받은 공식 설치기의 SHA-256 확인 후 승인 (승인 뒤에만 --yes)
G1   LLM 플랜              가입 링크 → 키를 숨김 입력 또는 hermes setup --portal(OAuth)
                          → 모델 테스트 응답이 와야 통과
G2   Telegram 봇 토큰       @BotFather → 숨김 입력 → getMe 성공해야 통과
G3   내 사용자 ID           @userinfobot → 숨김 입력 → 숫자 ID 목록이어야 통과
G5   (선택) 개인 메일       주소 저장으로 선택 → 앱 비밀번호 숨김 입력 → IMAP·SMTP 로그인이 둘 다 돼야 통과
────────────────────────
스테이지 순서: prep → hermes → llm → telegram → gateway → wiki → harness → cron → mail → verify
```

키·토큰은 사용자가 자기 터미널에서 `bash bootstrap/bin/oaos-secret-set KEY`로 입력합니다(화면·채팅·로그·명령행 인자에 값이 남지 않고, Hermes `.env`에 소유자 전용 600으로 저장). 에이전트가 읽고·설치하고·검증합니다. 사용자는 게이트만 처리합니다.

`wiki` 스테이지는 `~/data/wiki`(git 저장소·색인 시드·비밀값 차단 pre-commit 훅)와 함께 로컬 bare 중계 저장소 `~/data/wiki.git`(소유자 전용 700)을 만들어 위키의 `origin`으로 연결하고 시드 커밋을 push합니다. 위키에 이미 다른 `origin`이 있으면 그대로 두고 중계를 `oaos-relay` 원격으로 추가합니다. 위키 폴더가 사라졌는데 중계에 기록이 남아 있으면 새로 시드하지 않고 중계에서 복원합니다. 다른 기기에서는 SSH로 `git clone <사용자>@<호스트>:data/wiki.git`처럼 중계를 받아 쓸 수 있습니다.

`cron` 스테이지는 일일 백업을 Hermes cron(`oaos-daily-backup`)에, 게이트웨이 워치독을 OS 타이머(Linux systemd 사용자 타이머, macOS launchd 에이전트)에 등록합니다. Hermes cron은 게이트웨이 안에서 돌기 때문에 게이트웨이가 죽으면 cron 워치독도 멈추기 때문입니다. Windows는 별도 워치독 없이 Hermes ONLOGON 태스크에 의존합니다. 일일 백업은 아카이브를 만든 뒤 위키의 현재 브랜치를 중계에 push합니다. 이전 릴리스가 만든 백업 스크립트는 그대로 일치할 때만 자동으로 새 버전으로 바뀌고, 사용자가 고친 스크립트는 보존됩니다.

### G5 — 개인 메일(선택)

`mail` 스테이지는 선택 사항입니다. 메일 주소를 저장하기 전까지는 메일 서버에 접속하지 않고 `SKIP(not_selected)`으로 기록하며, 설치는 그대로 완료될 수 있습니다.

| 변수 | 종류 | 설명 |
|---|---|---|
| `OAOS_PERSONAL_MAIL_ADDRESS` | 비밀 아님 — 이 값을 저장하면 G5 선택 | 에이전트가 읽고 보낼 메일 주소(예: Gmail) |
| `OAOS_PERSONAL_MAIL_APP_PASSWORD` | 비밀 — 숨김 입력만 | 앱 비밀번호(공백 없이 한 단어). Gmail: 2단계 인증을 켠 뒤 https://myaccount.google.com/apppasswords 에서 생성 |
| `OAOS_PERSONAL_MAIL_IMAP_HOST` · `OAOS_PERSONAL_MAIL_SMTP_HOST` | 비밀 아님 — Gmail 외에만 | 기본값은 Gmail 주소일 때 `imap.gmail.com` · `smtp.gmail.com` |

```text
bash bootstrap/bin/oaos-secret-set OAOS_PERSONAL_MAIL_ADDRESS
bash bootstrap/bin/oaos-secret-set OAOS_PERSONAL_MAIL_APP_PASSWORD
bash editions/personal/install.sh --stage mail
```

저장 직후 `--stage mail`이 IMAP(993, TLS)·SMTP(587, STARTTLS) 로그인을 둘 다 시도합니다. 하나라도 실패하면 값은 저장된 채로 단계가 BLOCKED이고, 앱 비밀번호 링크와 재입력 명령을 다시 안내합니다. 비밀번호는 명령행 인자·로그·상태 파일·출력에 남지 않습니다(프로세스 환경 변수로 넘긴 값은 무시).

로그인이 성공하면 Himalaya 1.x(https://pimalaya.org/himalaya/)가 설치된 Linux·macOS에서는 `~/.config/himalaya/config.toml`(600)에 `oaos-personal` 계정을 씁니다. 이 설정에는 비밀번호가 없고, `auth.cmd`가 소유자 전용 비밀번호 명령 `~/.oaos/bin/oaos-mail-password`(700)를 가리킵니다. 이 명령은 실행될 때마다 Hermes `.env`에서 값을 읽습니다. 사용자가 직접 만든 Himalaya 설정은 덮어쓰지 않습니다. Himalaya가 없거나 1.x가 아니면, 또 Windows 레인에서는 설정을 MANUAL로 안내합니다. 설치한 뒤 `bash editions/personal/install.sh --stage mail --recheck`를 다시 실행하거나, 직접 구성한 경우 확인 후 `--confirm mail`로 기록합니다.

에이전트는 저장소에서 `bash editions/personal/install.sh --dry-run`으로 계획을 확인한 뒤 `bash editions/personal/install.sh`를 실행합니다(Windows는 **Git Bash에서 실행**). Ubuntu는 공식 Hermes `install.sh`·systemd, macOS는 공식 `install.sh`·launchd, Windows는 공식 `install.ps1`·ONLOGON 태스크/Startup 폴백을 사용하도록 구현했습니다. apt·swapfile·systemd·`/proc` 명령은 Ubuntu 경로에만 적용됩니다. macOS·Windows 시간대와 절전/로그인 정책은 필요 시 수동 확인합니다.
최종 확인은 `bash bootstrap/verify/personal-verify.sh`(21항목 — 20 개인 메일, 21 위키 중계 포함)로 실행합니다. 종료 코드는 0 = 모두 PASS, 2 = FAIL 없음·MANUAL 대기, 1 = FAIL입니다. 재부팅(또는 로그아웃·로그인) 생존, 허용 계정 메시지 왕복, 비허용 계정 무응답은 사람이 확인한 뒤 `--confirm reboot,chat,allowlist`로 기록합니다. 개인 메일 행은 G5 미선택이면 SKIP, 로그인 실패면 FAIL, Himalaya를 사용자가 직접 설치·구성해야 하면 MANUAL(`--confirm mail`)입니다. 위키 중계 행은 중계가 위키의 최신 커밋을 갖고 있어야 PASS이며, 뒤처지면 push 명령과 함께 FAIL로 알립니다. 하네스의 `{{...}}` 자리표시자는 시딩 대화로 채울 때까지 MANUAL입니다. 설치기는 MANUAL이 남아 있으면 verify를 `applied`로 기록하고 2로 끝납니다.

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

다음 원격 대시보드 예시는 **Ubuntu/systemd 서버 구성**입니다. Personal을 macOS·Windows에 네이티브 설치한 경우의 대시보드 자동 시작·원격 연결은 별도 실기기 확인이 필요합니다. 쓰는 PC에 Hermes 데스크탑 앱을 설치하면 그래픽 화면에서 연결할 수 있습니다. 앱 내려받기: [Hermes 데스크탑](https://hermes-agent.nousresearch.com/desktop) (Windows·macOS·Linux)

**1. 서버 준비 — 대시보드는 루프백(127.0.0.1)에만 열기 (기본값)**

대시보드 로그인 계정을 Hermes `.env`에 숨김 입력으로 저장합니다(값이 화면·인자에 남지 않음).

```text
bash bootstrap/bin/oaos-secret-set HERMES_DASHBOARD_BASIC_AUTH_USERNAME
bash bootstrap/bin/oaos-secret-set HERMES_DASHBOARD_BASIC_AUTH_PASSWORD
openssl rand -base64 32 | bash bootstrap/bin/oaos-secret-set HERMES_DASHBOARD_BASIC_AUTH_SECRET
```

대시보드는 기본 바인딩(127.0.0.1)으로 구동합니다. `--host 0.0.0.0`은 쓰지 않습니다 — 검증기의 `Dashboard loopback` 항목이 비루프백 리스너를 FAIL로 판정합니다.

```text
hermes dashboard --port 9119 --no-open
```

**2. 쓰는 PC에서 SSH 터널 열기**

```text
ssh -N -L 9119:127.0.0.1:9119 <사용자>@<서버 IP>
```

**3. 앱에서 연결 — Settings → Gateways → Add connection → Remote gateway**

- Remote URL에 `http://127.0.0.1:9119`(터널 끝) 입력 → **Sign in**에서 위 사용자 이름·비밀번호로 로그인
- **Test**로 연결을 확인합니다 — 로그인은 한 번만 하면 세션이 유지됩니다

**4. 인터넷에서도 쓰려면 (선택)**

포트를 직접 열지 말고 SSH 터널이나 VPN(예: Tailscale)으로 접속합니다. HTTPS 리버스 프록시가 꼭 필요하면 같은 호스트의 프록시가 127.0.0.1:9119로 전달하게 하고, 노출 범위를 별도로 검토합니다.

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
