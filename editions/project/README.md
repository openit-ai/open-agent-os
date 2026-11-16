# Project Edition — 팀용 에이전트

> 작은 팀(2~6인)을 위한 팀 어시스턴트. 팀 채팅·지식·메일을 하나의 에이전트가 잇습니다.

[← README로 돌아가기](../../README.md) · [쿡북](../../docs/cookbook.md) · [FAQ](../../docs/faq.md)

## 이런 분께

- 팀 채팅·문서·메일이 흩어져 맥락을 다시 모으는 데 시간을 쓰는 팀
- 팀 지식이 사람이 그만둘 때 같이 사라지는 게 아쉬운 팀
- 외주 SaaS가 아니라 직접 통제하는 팀 비서가 필요한 팀

## 구성

**Personal 기반 + 추가**

- Ubuntu 22.04 또는 24.04 LTS VPS 1대 — 4 vCPU / 16GB / 200GB (예: 프로모션 기준 월 약 ₩16,000대, 2026-09 확인)
- Mattermost — 팀 채팅 창구 (+ Telegram 병행)
- Outline — 팀 문서·지식베이스
- 봇 전용 메일함 — 팀 메일 확인·초안·전달
- 팀 계정 — 멤버별 접근 통제(허용목록/페어링)
- 도메인 + HTTPS (nginx + TLS)

## 설치 흐름 — 필수 게이트 8개

```text
선택  에디션 선택          "Project로 진행할까요?"
G0  서버 준비            VPS 구매 → 그 VPS에 Hermes 설치 → 에이전트에게 이 저장소 URL 전달
G1  LLM 플랜             가입 링크 → 키를 내 터미널에서 숨김 입력
G2  Telegram 봇 토큰     @BotFather 토큰 숨김 입력
G3  Telegram 사용자 ID   숫자 ID 숨김 입력
G6  도메인 DNS           A 레코드 3개(chat/note/portal) → 서버 IP
G9  봇 메일함            전용 메일 계정 생성 → 앱 비밀번호 숨김 입력
G7  Mattermost 봇        사람 관리자 가입 → 봇 계정 생성 → 봇 토큰·허용 사용자 ID 숨김 입력
G8  Outline 에이전트 계정  사람 관리자 가입 → 에이전트 계정(봇 메일함) 초대 → 그 계정의 API 토큰 숨김 입력
────────────────────────
이후: 설치 → 팀 서비스 셋업 → 멤버 등록 → 검증 → 보고
```

Personal의 선택 게이트 G4(대용량 파일)·G5(개인 메일)는 Project에서 제공하지 않습니다. 메일은 G9 봇 전용 메일함만 연결합니다(개인 계정과의 정보보안 분리).

에이전트가 읽고·설치하고·파일을 고치고·검증합니다. 사용자는 게이트만 처리합니다. 모든 게이트 값은 사용자가 **자기 터미널에서** `bash bootstrap/bin/oaos-secret-set KEY`로 숨김 입력하며, 값은 Hermes `.env`(600)에만 저장되고 채팅·명령행 인자·로그·상태 파일에 남지 않습니다. 설정 파일을 직접 고칠 필요는 없습니다.

### G0 흐름

1. VPS(Ubuntu 22.04/24.04 LTS, 권장 4 vCPU / 16GB / 200GB)를 준비하고 SSH 키로 접속합니다.
2. 그 VPS에 Hermes를 설치하고, 거기서 실행되는 에이전트에게 이 저장소 URL을 전달합니다. 설치는 VPS 안에서 진행됩니다.
3. `prep` 스테이지가 사양을 점검합니다. 권장 사양 미만이면 WARN, 최소 사양(2 vCPU / 8GB / 60GB) 미만이면 BLOCKED입니다(`OAOS_ALLOW_SMALL_HOST=1`로 알고 진행 가능).
4. 80·443·3000·5432·6379·8065 포트를 다른 프로그램이 쓰고 있으면 BLOCKED로 멈춥니다.
5. SSH 키 로그인 여부와 비밀번호 로그인 허용 여부를 보고합니다(설정은 바꾸지 않음).
6. UFW에 SSH(22와 현재 SSH 포트)·80·443 허용 규칙을 **먼저** 넣은 뒤 UFW를 활성화합니다. 규칙이 먼저라 현재 SSH 세션이 끊기지 않습니다. 활성화를 미루려면 `OAOS_UFW_ENABLE=0`(검증에서 FAIL로 표시).

### 실행

에이전트는 Project VPS에서 `bash editions/project/install.sh --dry-run`으로 계획을 확인한 뒤 `bash editions/project/install.sh`를 실행합니다. 설치기와 `bash bootstrap/verify/project-verify.sh`는 같은 비루트 관리자 계정에서 실행하며 `sudo -n true`가 성공해야 합니다. 설치기는 공식 저장소(`github.com/openit-ai/open-agent-os`)에서만 실행됩니다(검토한 포크는 `OAOS_ALLOW_FORK=1`). 상태는 `~/.oaos-install/state.json`의 Project 전용 영역(`editions.project.stages`)에 저장되므로, 같은 호스트의 Personal 상태가 Project 단계를 건너뛰게 하지 않습니다. 완료된 단계는 건너뛰고 `verify`는 매번 다시 돕니다. `--recheck`는 완료 단계도 다시 실행하고, `--reverify`는 검증만 실행합니다. 종료 코드: 0 = 모두 PASS, 2 = FAIL 없이 MANUAL 대기, 3 = 게이트·전제 조건 대기, 1 = 오류 또는 검증 FAIL.

스테이지 순서는 `prep hermes llm telegram wiki harness cron stack ingress mail team gateway verify`입니다.

- `team`은 Mattermost·Outline 자격 증명을 검증해 Hermes `.env`에 저장합니다. 게이트웨이가 **이미 설치되어 실행 중일 때만** 재시작해 새 설정을 반영합니다. 새 서버에서는 아직 게이트웨이가 없으므로 재시작하지 않습니다.
- `gateway`는 `telegram`이 끝난 뒤 게이트웨이를 처음 설치·기동합니다. `team`이 먼저 끝났다면 Telegram과 Mattermost가 모두 들어간 상태로 첫 기동합니다. 그 전에 `approvals.cron_mode`·`single_query_mode`·`unattended_mode`를 `deny`로 설정합니다(무인 실행 승인 거부).
- `cron`은 일일 백업 작업 하나(`oaos-daily-backup`)를 등록합니다.

| 구성 요소 | 설치·운영 방식 | 경로 |
|---|---|---|
| PostgreSQL·Redis | Ubuntu apt, systemd, 127.0.0.1 전용 | PostgreSQL `listen_addresses='localhost'`(`conf.d/oaos-listen.conf`), Redis `bind 127.0.0.1` |
| Mattermost | 서명된 공식 APT 저장소, systemd, 127.0.0.1:8065 | `/opt/mattermost`; `mmctl`은 `/opt/mattermost/bin/mmctl` |
| Outline | 공식 v1.10.1 태그(커밋 `4a5a616a21be800257dc11cef4263d0dd0412156`), NodeSource Node.js 22(`/usr/bin/node`)·Corepack Yarn(`/usr/bin/yarn`) 소스 빌드, systemd, 127.0.0.1:3000 | `/opt/outline`, `/var/lib/outline/data`; 서비스 `oaos-outline` |
| 게이트 값·토큰 | 공용 시크릿 도우미, 소유자 전용 | Hermes `.env`(600) |
| 서비스 내부 시크릿(승인된 예외) | 설치기가 생성, 600 | `~/oaos/stack/.env`(DB 비밀번호·Outline 키·도메인), `/etc/oaos/outline.env`(root), `/opt/mattermost/config/config.json`(mattermost) |

Docker는 쓰지 않습니다. 모든 서비스는 네이티브 패키지와 systemd로 운영합니다.

Mattermost의 [서명 APT 방식](https://docs.mattermost.com/deployment-guide/server/linux/deploy-ubuntu)은 보안 업데이트 경로가 단순하여 채택했습니다. [공식 tarball·systemd 방식](https://docs.mattermost.com/deployment-guide/server/linux/deploy-tar)은 대안입니다. Outline 빌드·설정은 [v1.10.1 공식 소스](https://github.com/outline/outline/releases/tag/v1.10.1)를 기준으로 합니다. 설치된 패키지 버전은 `~/oaos/stack/versions.txt`에 남습니다.

Outline 3000 포트는 고정 태그의 `server/main.ts` 한 줄만 수정하여 `127.0.0.1`에 바인딩합니다. 소스·빌드 산출물·실제 리스너 검사를 통과하지 못하면 설치를 중단합니다. 태그를 업데이트할 때는 패치를 다시 적용하고 전부 재검증해야 합니다. upstream이 공식 바인딩 설정을 추가하면 검증 후 패치를 제거합니다. 실제 소스 빌드에서 바인딩 코드는 `build/server/main.js`에 들어가고 `build/server/index.js`가 이를 불러옵니다.

Personal에서 이전하려면 Project 서버에서 `bash bootstrap/migrate/personal-to-project.sh --source user@host --dry-run`으로 계획을 확인하고, 같은 명령을 `--dry-run` 없이 실행합니다. 시크릿 `.env`는 이전되지 않으므로 게이트 값을 다시 입력합니다.

### 게이트 값 (모두 `bash bootstrap/bin/oaos-secret-set KEY`로 입력)

| 게이트 | 키 | 설명·즉시 검증 |
|---|---|---|
| G1 | `<PROVIDER>_API_KEY` 또는 `hermes setup --portal` | 모델 테스트 요청이 응답해야 통과합니다. |
| G2 | `TELEGRAM_BOT_TOKEN` | Telegram `getMe`로 확인합니다(토큰은 curl 표준입력 설정으로만 전달). |
| G3 | `TELEGRAM_ALLOWED_USERS` | 숫자 ID만, 쉼표로 구분합니다. |
| G6 | `OAOS_BASE_DOMAIN` (또는 `OAOS_CHAT_DOMAIN`·`OAOS_NOTE_DOMAIN`·`OAOS_PORTAL_DOMAIN`), 선택 `OAOS_ACME_EMAIL` | 도메인은 필수입니다. `ingress`가 세 A 레코드가 서버 공인 IPv4를 가리킬 때까지 최대 10분(`OAOS_DNS_TIMEOUT`초) 기다립니다. 인증서 이메일은 따로 주지 않으면 G9 봇 메일 주소를 씁니다. 도메인을 바꾸려면 새 값을 저장하고 `OAOS_ACCEPT_DOMAIN_CHANGE=1 bash editions/project/install.sh --recheck --stage stack,ingress`를 실행합니다. OAOS가 만든 nginx vhost는 템플릿 해시를 기록해 두었다가 템플릿이나 도메인이 바뀌면 다시 생성하고 인증서를 재설치합니다. |
| G9 | `OAOS_MAIL_ADDRESS`, `OAOS_MAIL_PASSWORD`, (Gmail이 아니면) `OAOS_MAIL_IMAP_HOST`·`OAOS_MAIL_SMTP_HOST` | 봇 전용 메일함입니다. 앱 비밀번호는 공백 없이 입력합니다. Gmail 주소는 `imap.gmail.com`·`smtp.gmail.com`이 기본값입니다. `mail`이 IMAP·SMTP 로그인을 바로 확인하고 Himalaya 1.x와 Outline SMTP를 설정합니다. |
| G7 | `MATTERMOST_TOKEN`, `MATTERMOST_ALLOWED_USERS` | 아래 "봇 계정 원칙"을 따릅니다. 허용 사용자는 Mattermost 사용자 ID(26자) 목록이며 모두 실제로 존재해야 합니다. |
| G8 | `OUTLINE_API_TOKEN` | 아래 "봇 계정 원칙"을 따릅니다. 설치기는 Outline 주소를 `OUTLINE_URL`로 함께 저장합니다. |

예전 버전이 `~/oaos/stack/.env`에 넣었던 `MATTERMOST_TOKEN`·`OUTLINE_API_TOKEN`·`OAOS_MAIL_*` 값은 다음 실행 때 Hermes `.env`로 옮기고 stack 쪽 사본은 지웁니다. 비밀 값을 환경 변수(`OAOS_MAIL_PASSWORD=... bash install.sh` 등)로 넘기면 무시하고 경고합니다. 셸 기록에 남기 때문입니다.

`--stage`로 막힌 게이트부터 재시도합니다. 실제 송수신과 허용·비허용 팀 계정 동작은 에이전트와 함께 확인해야 합니다. 신규 VPS 실측 전에는 전체 설치 완료로 보고하지 않습니다.

### G7·G8 — 봇 계정 원칙

에이전트는 사람 계정이나 관리자 권한으로 움직이지 않습니다.

- **G7 Mattermost**: 사람이 `https://chat.<도메인>`에서 첫 관리자 계정을 만듭니다. 그 관리자가 System Console → Integrations → Bot Accounts에서 Hermes용 **봇 계정**을 만들고(시스템 관리자 역할 없음) 봇 토큰을 발급합니다. `team`은 `/api/v4/users/me`로 토큰이 봇(`is_bot=true`)이고 `system_admin` 역할이 없는지 확인하며, 사람 계정 토큰이나 관리자 봇 토큰은 거부합니다. 확인한 `mmctl --local` 버전은 봇·토큰을 만들지 못하므로 관리자 존재와 SiteURL 확인에만 씁니다.
- **G8 Outline**: wiki 작성·관리의 주체는 AI 에이전트입니다. 사람 관리자가 **먼저** 가입합니다(첫 계정 = 워크스페이스 관리자). 그 관리자가 Settings → Security에서 이메일 로그인을 켜고, **AI 에이전트 전용 계정**(이메일 = G9 봇 메일함)을 멤버 또는 편집자로 초대합니다. 에이전트 계정은 봇 메일함으로 온 로그인 링크로 들어가 Settings → API Keys에서 토큰(`ol_api_`로 시작)을 발급합니다. `team`은 `POST /api/auth.info`로 토큰 계정의 이메일이 봇 메일함과 같고 관리자가 아닌지 확인합니다. 봇이 만드는 문서는 이 계정 명의로 남고, 사람이 직접 고친 경우에만 그 사람의 기록이 남습니다.

### Outline 첫 관리자 로그인과 신원 제공자(IdP)

실측한 v1.10.1은 아직 팀이 없을 때 이메일 매직링크 로그인을 제공하지 않습니다(`/api/auth.config`가 빈 목록). 첫 관리자는 다음 중 하나로 만듭니다.

1. **권장**: 조직의 신원 제공자(OIDC/Slack/Google 등)를 붙입니다. 값은 `OUTLINE_` 접두어를 붙여 숨김 입력합니다. 예: `OUTLINE_OIDC_CLIENT_ID`, `OUTLINE_OIDC_CLIENT_SECRET`, `OUTLINE_OIDC_AUTH_URI`, `OUTLINE_OIDC_TOKEN_URI`, `OUTLINE_OIDC_USERINFO_URI`, `OUTLINE_SLACK_CLIENT_ID`, `OUTLINE_GOOGLE_CLIENT_ID` 등. 그다음 `bash editions/project/install.sh --recheck --stage mail`(또는 `stack`)을 실행하면 설치기가 `/etc/oaos/outline.env`에 접두어 없는 이름(`OIDC_CLIENT_ID` 등)으로 쓰고 Outline을 재시작합니다. 운영자가 `/etc/oaos/outline.env`에 직접 추가한 다른 키도 다시 생성할 때 보존됩니다.
2. 신원 제공자를 붙일 수 없는 검증·테스트 환경이라면, 무인증 설치 엔드포인트 `POST /api/installation.create`(`{"teamName","userName","userEmail"}`)로 첫 팀·관리자만 만듭니다(운영 환경에서는 쓰지 않습니다).

첫 관리자가 생긴 뒤 이메일 로그인을 켜면 G9 SMTP 설정으로 에이전트 계정이 로그인 링크를 받을 수 있습니다. 토큰 문자열은 `ol_api_` 뒤 영숫자·밑줄 38자이며, 다른 문자가 섞이면 `Unable to decode token`(401)이 납니다.

### 백업과 복원

`oaos-daily-backup`(매일 03:00, Hermes 예약 작업)은 `~/.oaos/backups/oaos-project-<시각>.tar`(폴더 700, 파일 600, 최근 7개 보관)를 만듭니다. 포함 항목: Hermes 홈, `~/data/wiki`, `~/oaos/stack`, PostgreSQL `mattermost`·`outline` 덤프(`pg_dump -Fc`), `/etc/oaos`, Mattermost `config.json`, `/opt/mattermost/data`, `/var/lib/outline/data`. 백업에는 시크릿이 들어 있으므로 서버 밖으로 옮길 때는 암호화하세요.

복원은 `bash editions/project/restore.sh --dry-run <아카이브>`로 모든 부분을 먼저 점검하고(아무것도 바꾸지 않음), `--yes`로 실행합니다. 실행 시 게이트웨이·Outline·Mattermost를 멈추고, 현재 Hermes 홈·위키·stack 폴더를 `*.pre-restore-<시각>`으로 옮긴 뒤 복원하고 서비스를 다시 시작합니다. 검증기는 매번 실제 백업 작업을 실행하고 그 결과물로 복원 dry-run을 확인합니다.

### 검증

`bash bootstrap/verify/project-verify.sh`(`--json`, `--offline`)는 Personal과 같은 Hermes 항목에 더해 DNS·TLS·HTTPS, nginx, 서비스 상태, 5432·6379·8065·3000 루프백 전용 리스너, PostgreSQL `listen_addresses`, 서비스 시크릿 파일 권한, UFW(활성 + 22·80·443), 무인 실행 승인 거부, Telegram getMe, 게이트웨이의 Telegram·Mattermost 연결, Mattermost 봇·허용 목록, Outline 에이전트 토큰·`OUTLINE_URL`, 봇 메일함 IMAP·SMTP 로그인, 백업·복원 dry-run을 확인합니다. 값이 있는데 메일 로그인이 실패하면 FAIL입니다. 사람이 확인해야 하는 항목(재부팅 후 생존, Telegram 허용·비허용, Mattermost 허용·비허용, 에이전트의 Outline 문서 작성, 메일 송수신)은 MANUAL이며, 확인 후 `--confirm reboot,chat,allowlist,mm-chat,mm-deny,outline-page,mail-flow`로 기록합니다. 종료 코드는 0 PASS / 2 MANUAL 대기 / 1 FAIL입니다.

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

Project 설치·검증·백업/복원·Personal 이전 스크립트가 제공됩니다. 신규 VPS에서 전체 게이트와 실제 서비스 동작을 검증해야 P1 완료로 판정합니다. Mattermost 봇(G7), Outline 에이전트 계정·API 토큰(G8), 봇 메일함(G9)은 사용자 게이트입니다. Hermes에는 아직 Outline 전용 스킬·MCP가 없으므로, 에이전트가 Outline API를 쓰려면 `OUTLINE_URL`·`OUTLINE_API_TOKEN`을 사용하는 스킬을 별도로 구성해야 합니다.

## 라이선스

**Apache License 2.0** — 오픈소스. 자유롭게 사용·수정·배포할 수 있습니다(상업적 사용 포함). [`LICENSE`](../../LICENSE) 참조.

## 관련 문서

- [부트스트랩 스킬](../../skills/oaos-bootstrap/SKILL.md) · [게이트 레퍼런스](../../skills/oaos-bootstrap/references/gates.md)
- [FAQ](../../docs/faq.md) · [설계서](../../docs/architecture-v2.0.md)
