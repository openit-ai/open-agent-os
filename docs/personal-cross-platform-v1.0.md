# Personal 크로스플랫폼 지원 설계 v1.0

> **상태(2026-09-27)**: 설계 정본. Personal P1~P3의 Ubuntu(Linux)·macOS Apple Silicon·Windows 11 Git Bash 스크립트 구현과 로컬 모의 검증 완료. P4 CI 매트릭스·스킬·문서를 추가했고 호스티드 러너 3종(ubuntu-latest·macos-latest·windows-latest) 매트릭스를 통과했다(2026-09-27). 실기기 설치·키/메시지·로그인/재부팅 검증 P5는 미완이다. Project·Company는 Ubuntu LTS 서버 경로를 유지한다.

## 0. 범위·근거

OAOS(Open Agent OS) Personal의 지원 OS는 Hermes Agent의 네이티브 설치 가능 OS 중 **Ubuntu(Linux), macOS Apple Silicon, Windows 11**이다. Project·Company의 서버 설치 경로는 Ubuntu LTS로 유지한다. Windows 10은 Hermes Tier 1이지만 OAOS Personal 정책 범위 밖이다. Intel Mac은 Hermes 문서에 별도 빌드 경로가 언급되어도 Tier 1의 Apple Silicon 범위 밖이므로 이 설계의 지원 대상이 아니다. WSL2·Docker·Nix·Android 역시 Personal 네이티브 3개 레인에 포함하지 않는다. 지원 범위는 하드웨어가 아닌 Hermes와 OAOS의 실제 설치·검증 범위가 모두 충족될 때 구현 완료로 표기한다. [Hermes 플랫폼 지원](https://hermes-agent.nousresearch.com/docs/getting-started/platform-support)

| OAOS Personal 대상 | 아키텍처 | Hermes 설치 경로 | 게이트웨이 지속성 | 현재 OAOS 구현·검증 |
|---|---|---|---|---|
| Ubuntu LTS | x86_64·aarch64(Hermes Linux Tier 1) | 공식 `install.sh` | `hermes gateway install` → systemd 사용자 서비스; 부팅 전 상주가 필요하면 linger | Linux 설치·검증 스크립트 있음. 해당 Ubuntu 버전·아키텍처별 실설치 증거는 별도 확인 필요 |
| macOS | Apple Silicon(arm64) | 공식 `install.sh` 또는 Hermes Desktop | `hermes gateway install` → launchd 사용자 에이전트, 로그인 후 시작 | 미구현·미검증 |
| Windows 11 네이티브 | x86_64·aarch64 | 공식 `install.ps1` 또는 Hermes Desktop/MSIX(해당 패키지의 OS 요건 확인) | `hermes gateway install` → 로그온 태스크 `Hermes_Gateway`; 등록 실패 시 Hermes의 Startup 폴백 가능 | 미구현·미검증 |

Hermes Desktop 경로에서는 OAOS가 호출할 `hermes` CLI와 Git Bash가 실제 제공되는지 먼저 read-back한다. Windows MSIX는 공식 문서상 Windows 11 **22H2 이상** 요건이 있으므로 Windows 11 전체에 MSIX를 무조건 적용하지 않는다. Windows ARM64 선택 기능의 제한도 공식 매트릭스에 따르며 Personal 필수 기능에 영향이 있는지 실기기에서 확인한다. [Hermes 플랫폼 지원](https://hermes-agent.nousresearch.com/docs/getting-started/platform-support), [Windows 네이티브 가이드](https://hermes-agent.nousresearch.com/docs/user-guide/windows-native)

### 0-1. 사실과 설계 제안의 경계

- Hermes Windows 가이드의 2026-09-27 확인본: 터미널 도구는 **Git Bash**로 셸 명령을 실행한다. 설치기는 기본적으로 PortableGit을 `%LOCALAPPDATA%\hermes\git`에 프로비저닝하고 `HERMES_GIT_BASH_PATH`를 명시적으로 설정한다. `bash.exe` 탐색 순서는 해당 변수 → `%LOCALAPPDATA%\hermes\git\usr\bin\bash.exe` → `%LOCALAPPDATA%\hermes\git\bin\bash.exe` → `%ProgramFiles%\Git\bin\bash.exe` 등 시스템 Git 경로다. MinGit을 수동 배치한다면 Bash를 포함한 **non-busybox** 변형이 필요하다(BusyBox 변형은 `ash`만 제공). OAOS는 설치 버전·사용자 설정에 따른 차이를 고려해 특정 변수·경로만 가정하지 않고 실제 `bash.exe` 기능을 검사하며, Bash가 아닌 `ash`는 차단한다. [Windows 네이티브 가이드](https://hermes-agent.nousresearch.com/docs/user-guide/windows-native)
- Hermes 공식 확인: Windows 소스 설치 기본 데이터 홈은 `%LOCALAPPDATA%\hermes`(`HERMES_HOME`으로 변경 가능), 소스 코드는 `%LOCALAPPDATA%\hermes\hermes-agent`, CLI 런처는 `%LOCALAPPDATA%\hermes\bin`이며 User PATH 반영에는 새 터미널이 필요하다. MSIX 전용 설치에는 소스 체크아웃이 없을 수 있다. [Windows 네이티브 가이드](https://hermes-agent.nousresearch.com/docs/user-guide/windows-native)
- 아래 `platform_*` API, OS별 prep, 상태 전이는 **OAOS 설계 제안**이다. macOS·Windows 명령의 실행 결과, 파일 권한의 보안 효과, 로그인·재부팅 지속성은 아직 확인 필요다. 문서 경로는 홈(`~`)·저장소 상대경로를 쓰고, Windows 고유 경로는 환경 변수로 표기한다. [기존 문서 표기 원칙 §0-1](architecture-v2.0.md)

## 1. 아키텍처 결정

**Personal 설치기·검증기는 단일 Bash 코드베이스와 명시적 `linux` / `macos` / `windows-gitbash` 분기를 쓴다.** Windows에서도 Hermes 터미널 도구의 실제 셸은 Git Bash다. Bash 공통 흐름은 단계·게이트·상태 파일·결과 형식을 하나로 유지하고, OS 의존 동작만 `bootstrap/lib/platform.sh`로 내린다. Windows에서 공식 Hermes 설치기를 실행해야 할 때만 PowerShell 호출을 플랫폼 구현 내부에 가둔다. Project·Company 코드는 이 리팩터의 대상이 아니다.

| 대안 | 이점 | 비용·위험 | 결정 |
|---|---|---|---|
| 단일 Bash + OS 분기 | 기존 Personal 단계·테스트 재사용; Hermes Windows 실행 셸과 일치 | Bash 3.2·BSD·MSYS 이식성 검사 필수 | **채택** |
| Windows PowerShell 별도 레인 | Windows API·ACL 접근 쉬움 | 상태·게이트·검증 로직 이중화; Hermes 터미널 셸과 경계가 늘어남 | 설치기 전체에는 미채택; OS 프리미티브에서만 필요 시 호출 |
| OS별 완전 분리 스크립트 | 초기 분기 작성이 단순 | Linux 수정이 다른 레인에 누락되기 쉽고 checkpoint 의미가 갈라짐 | 미채택 |

### 1-1. 플랫폼 계층 계약

`bootstrap/lib/platform.sh`는 Personal 진입점에서만 로드한다. `os_detect`는 실행 OS·CPU·Git Bash 정체를 실제 호스트에서 판별해 세 레인 중 하나를 반환한다. 미지원 조합은 prep 이전에 오류로 종료한다. 테스트용 `OAOS_TEST_PLATFORM` 같은 오버라이드는 명시적 테스트 모드에서만 허용하고 실제 설치에서 신뢰하지 않는다. 플랫폼 함수는 표준 출력에 비밀값을 쓰지 않고, 읽기 전용 조회와 상태 변경을 구분한다. 변경 함수는 기존 `--dry-run`을 지켜 파일·서비스·상태를 바꾸지 않는다.

| 프리미티브(제안 API) | Linux | macOS | Windows Git Bash |
|---|---|---|---|
| `os_detect`, `platform_arch`, `platform_require_tools` | 커널·아키텍처 확인; Ubuntu 권장/지원 범위 검사 | Darwin + arm64 확인 | Windows/MSYS 식별 + x86_64/aarch64 확인; 실제 Git Bash·CLI 접근 검사 |
| `platform_hermes_home`, `platform_oaos_home`, `platform_path` | Hermes 기본 `~/.hermes`; OAOS `$HOME` | Hermes 기본 `~/.hermes`; OAOS `$HOME` | Hermes는 `HERMES_HOME` 우선, 기본 `%LOCALAPPDATA%\hermes`; OAOS는 Git Bash `$HOME`. Windows 네이티브 경로와 MSYS 경로 변환을 한 함수에 제한 |
| `platform_package_prereqs`, `platform_install_hermes` | 기존 apt 검사·공식 `install.sh` | 필요한 도구만 검사하고 공식 `install.sh`로 연결; OS 패키지 설치 정책 확인 필요 | Git for Windows Bash·필수 도구 검사; 공식 `install.ps1`은 PowerShell에서 실행. 관리자 권한을 기본 요구하지 않음 |
| `platform_memory_kib`, `platform_swap_state`, `platform_disk_kib` | `/proc/meminfo`, `/proc/swaps`, `df -Pk` | 시스템 메모리·swap 사용량은 macOS 시스템 조회로, 디스크는 `df -Pk`; 정확한 호출·단위는 P2 실측 | Windows 시스템 조회로 총 RAM·pagefile·대상 볼륨 여유량 확인; PowerShell/CIM 호출·단위는 P3 실측 |
| `platform_timezone_get`, `platform_timezone_set` | `timedatectl` | macOS 시스템 시간대 조회·변경; 권한과 호출은 P2 확인 | Windows 시간대 조회·변경; Windows ID와 IANA `Area/City` 매핑은 P3 확인. 모호한 매핑은 자동 적용 금지 |
| `platform_swap_prepare`, `platform_sleep_policy` | 기존 16 GiB 미만/스왑 없음 → 8 GiB swapfile·`fstab`; logind·sleep mask | OS 관리 가상메모리 사용. Linux swapfile 생성·sleep mask 적용 금지; 절전 정책은 안내/수동 검증 | OS 관리 pagefile 사용. Linux swapfile 생성·sleep mask 적용 금지; 전원·로그온 정책은 안내/수동 검증 |
| `platform_secret_protect`, `platform_secret_check` | 파일 mode 600·디렉터리 접근 확인 | POSIX mode 600·소유자 확인 | NTFS ACL에서 현재 사용자와 필수 시스템 주체 외 접근을 제한·read-back. MSYS `chmod 600` 표시만으로 PASS 금지; 정책·파일시스템 제약 시 BLOCKED |
| `platform_sha256`, `platform_file_mode`, `platform_atomic_write` | `sha256sum`, `stat -c`, 기존 원자적 rename | `shasum -a 256`, `stat -f`, 동일 파일시스템의 임시 파일·rename | 가용 해시 도구 확인 후 검증; ACL·rename 동작 실측. 고정 GNU 유틸 가정 금지 |
| `platform_gateway_install`, `platform_gateway_status`, `platform_gateway_autostart` | `hermes gateway install/status`; 필요 시 systemd enabled·linger read-back | `hermes gateway install/status`; launchd 등록·실행 및 로그인 후 read-back | `hermes gateway install/status`; schtasks 또는 공식 Startup 폴백 등록·실행 및 로그온 후 read-back |

게이트웨이 설치·시작·중단의 소유자는 Hermes다. OAOS는 `hermes gateway install`, `hermes gateway start`, `hermes gateway status`를 우선 사용하고, `systemctl`·`launchctl`·`schtasks`는 OS별 **등록 진단·교차 확인**에만 쓴다. Windows에서 `hermes gateway install`은 관리자 권한 없이 schtasks ONLOGON 태스크를 등록하고, 등록 실패 시 Hermes의 Startup 폴더 폴백을 사용한다. **스크립트·비TTY에서 `hermes gateway start`만 실행하면 로그인 자동시작은 등록되지 않으므로 OAOS는 `hermes gateway install`을 명시적으로 호출한다**(필요 시 `HERMES_GATEWAY_INSTALL_START_ON_LOGIN=1`). 상태 문자열의 `running|active` grep만으로 등록·재시작 정책을 판정하지 않는다. Windows 태스크 이름은 기본 프로필 `Hermes_Gateway`이며 프로필별 이름은 Hermes status가 보고한 실제 값을 따른다. 태스크 생성이 정책상 막혀 Hermes가 Startup 폴백을 쓴 경우도 status와 실제 로그온 테스트로 판정한다. Windows의 외부 강제 종료 후 자동 재시작은 보장되지 않으므로 watchdog의 기대 동작을 별도로 검증한다. [Hermes 게이트웨이 서비스 관리](https://hermes-agent.nousresearch.com/docs/user-guide/messaging), [Windows 네이티브 가이드](https://hermes-agent.nousresearch.com/docs/user-guide/windows-native)

### 1-2. 셸·유틸리티 이식성 장부

macOS 시스템 Bash 3.2를 최소 문법 대상으로 둔다. 현재 조사 범위 `bootstrap/lib/common.sh`, `editions/personal/install.sh`, `bootstrap/verify/personal-verify.sh`에는 연관 배열(`declare -A`), `mapfile/readarray`, `${var,,}`, nameref 등 Bash 4+ 전용 구문이 없다. 사용 중인 배열, `[[ ]]`, `=~`, `BASH_SOURCE`, process substitution, here-string, `read -d ''`, `set -Eeuo pipefail`은 Bash 3.2 문법 대상이다. 이는 **정적 조사**이며 macOS Bash 3.2 실행 통과는 P2에서 확인한다. 함수·테스트 추가 때 Bash 3.2 문법과 ShellCheck 범위를 고정한다.

| 차이 | 현재 사용·위험 | 후속 구현 계약 |
|---|---|---|
| `seq` | Personal 대상 스크립트에서 현재 호출 없음. Windows Git Bash 구성에 따라 유무 확인 필요 | 새 반복문은 Bash 산술 루프 사용; `seq` 의존 금지 |
| `sha256sum` / `shasum` | 설치기의 내려받은 파일 SHA-256 검사에 `sha256sum` 고정 | `platform_sha256`이 해시값만 반환. macOS `shasum -a 256`; Windows 도구 탐지·검증. 기대값 비교·승인 흐름 유지 |
| `stat -c` / `stat -f` | Personal 현재 직접 호출 없음; 권한·시간 검사를 추가하면 갈라짐 | 플랫폼별 mode/mtime 함수. 출력 형식에 의존하는 호출을 본문에 넣지 않음 |
| `sed -i` | 현재 Personal 직접 호출 없음; BSD와 GNU 인자 차이 | 임시 파일에 쓰고 원자적 교체; in-place 옵션 회피 |
| `readlink -f` | 현재 Personal 직접 호출 없음; macOS 기본 구현 차이 | 경로 정규화는 물리 경로 조회/플랫폼 함수로 제한; 없는 옵션 호출 금지 |
| `find -printf`, `sort -z`, `tail -z` | 검증기의 백업 정리에서 GNU 옵션 사용 | Python 3 또는 이식 가능한 파일 목록 처리로 교체; 공백·개행 경로 테스트 |
| `timeout 30s` | 모델 응답 검사에 사용; macOS 기본 제공 여부 미확인 | Python 3 timeout 또는 가용 도구 선택; 무한 대기 금지 |
| `df -Pk`, `awk`, `grep`, `tar`, `mktemp`, `git init -b` | 출력·버전·경로 의미가 OS/배포 도구마다 달라질 수 있음 | 파싱 결과를 단위·범위 검증; Git 옵션·tar 기능을 실제 환경에서 smoke 확인 |
| `/proc`, `apt-get`, `dpkg-query`, `timedatectl`, `systemctl`, `loginctl` | prep·서비스·검증에 직접 사용 | Linux 분기로만 이동; 다른 레인에서 호출되면 테스트 실패 |
| `/usr/bin/*` PATH 구성 | `bootstrap/tests/install-smoke.sh`의 Linux형 mock 전제 | macOS·Windows 테스트는 실제 PATH와 고립된 mock 디렉터리를 사용 |

Windows Git Bash 명령 가용성은 **설치 전 탐지 계약**이다. Git Bash가 있다는 사실만으로 아래 모든 명령이 있음을 가정하지 않는다.

| 명령군 | Linux | macOS | Windows Git Bash 계약 |
|---|---|---|---|
| `bash`, `git`, `curl` | prep/install 필수 | prep/install 필수 | `bash.exe`(Git for Windows), `git`, `curl`을 `command -v`와 실제 호출로 검사. 누락 시 공식 설치 경로 또는 사용자 조치로 BLOCKED |
| `python3`, `tar`, `mktemp`, `awk`, `grep`, `sed` | 상태 JSON·검증·백업에 사용 | 실제 가용성 확인 | Git Bash 제공 여부를 가정하지 않음; Hermes 관리 Python의 외부 스크립트 접근도 별도 확인. 대체 구현 전에는 BLOCKED |
| `jq`, `sha256sum`, `shasum`, `timeout`, `seq` | 일부 선택 또는 현재 사용 | 혼재·부재 가능 | 탐지 후 명시적 대체 경로. `jq`는 기존 Python 폴백 유지; `seq` 신규 사용 금지 |
| `powershell.exe`, `schtasks.exe`, ACL 조회 도구 | 해당 없음 | 해당 없음 | OS 프리미티브에서만 호출; 실행 실패·정책 제한은 안전하게 BLOCKED |

## 2. 설치 파이프라인·상태 계약

단계 순서와 공개 CLI는 유지한다: `prep → hermes → llm → telegram → gateway → wiki → harness → cron → verify`; `--stage`, `--status`, `--dry-run`, `--yes`, `--timezone`, `--skip-verify`와 종료 코드 `0=선택 단계 완료`, `3=게이트/전제 차단`, `1=실패`를 승계한다. G1 LLM 키·모델, G2/G3 Telegram bot token·allowed users는 OS와 무관한 동일 게이트이며 실제 모델 응답·Telegram 왕복 없이는 검증 완료가 아니다. `--dry-run`은 무변경 계획 출력이며 완료 증거가 아니다.

현재 `common.sh` 상태 파일은 `pending/done/blocked/failed`이고 성공 시 바로 `done`을 쓴다. 후속 구현에서는 **기존 `done`을 설치 적용 기록으로 보존**하고, 신규 결과는 `applied`(구성 적용), `verified`(OS별 read-back 및 필요한 수동 증거 완료), `SKIP`(명시적으로 허용된 선택 항목의 사유 기록), `blocked`, `failed`로 분리한다. `applied`는 재실행 시 read-back을 다시 하고, `verified`도 OS·Hermes 홈·서비스 등록이 바뀌면 재검증한다. 기존 `done`을 새 OS에서 자동 `verified`로 승격하지 않는다. 필수 `prep/hermes/llm/telegram/gateway/wiki/harness/cron/verify`의 무증거 SKIP은 금지한다. `--skip-verify`는 verify 수행을 미루며 설치 전체를 verified로 만들지 않는다. 상태 형식 변경 시 기존 Linux 상태 파일을 읽는 마이그레이션과 재개 테스트를 포함한다. 이 상태 분리는 아직 미구현이다.

OAOS 상태 파일 기본 경로는 **Git Bash에서 보이는** `~/.oaos-install/state.json` (`$HOME/.oaos-install/state.json`)이다. Windows에서 `$HOME`이 사용자 프로필로 해석되는 일반 구성이라면 대응 표기는 `%USERPROFILE%\.oaos-install\state.json`이지만 실제 매핑은 `os_detect`와 `--status`에서 read-back하며 고정 경로로 가정하지 않는다. 로그·백업·위키의 기본 OAOS 경로는 각각 `~/.oaos/logs`, `~/.oaos/backups`, `~/data/wiki`이고 Hermes 파일·하네스의 기준은 `HERMES_HOME`이다. Windows 기본 `HERMES_HOME`은 `%LOCALAPPDATA%\hermes`이므로 현행 `~/.hermes` 하드코딩을 그대로 사용하지 않는다. 상태·로그·백업 파일은 시크릿 원문을 포함하지 않는다. 상태 파일의 원자적 쓰기, 손상 파일 보존, 재실행 멱등성, 기존 데이터 보존을 유지한다.

| prep 항목 | Linux 현행/유지 | macOS 설계 | Windows 11 설계 |
|---|---|---|---|
| 패키지·Hermes | apt로 `git curl xz-utils ca-certificates`; 공식 `install.sh` | Bash·Git·curl·Python 등 필수 도구 탐지; 공식 `install.sh`; 필요한 패키지 설치 방식은 확인 필요 | Git for Windows Bash와 필요한 명령 탐지; 공식 `install.ps1`을 PowerShell에서 실행. `hermes` User PATH 새 셸 확인 |
| RAM·스왑 | 16 GiB 미만·스왑 없음이면 8 GiB swapfile과 `/etc/fstab` | 시스템 관리 swap 상태 확인; Linux 방식 변경 금지. 최소 자원 판정 기준 적용 가능성 확인 필요 | RAM·pagefile 조회; Linux 방식 변경 금지. pagefile 자동 변경 여부·권한은 확인 필요 |
| 시간대·전원 | `timedatectl`; logind drop-in과 sleep target mask | 시간대는 OS 방식으로 조회/설정; 로그인·절전 조건은 안내 및 수동 확인 | Windows 시간대 ID 매핑을 검증한 경우만 설정; 로그인·절전 조건은 안내 및 수동 확인 |
| 디렉터리·시크릿 | OAOS 디렉터리 생성; mode 600 | `$HOME`/`HERMES_HOME` 구분; mode 600·소유자 확인 | `$HOME`/`%LOCALAPPDATA%\hermes` 구분; ACL read-back. `chmod 600`만으로 통과 금지 |

Hermes 설치 다운로드의 SHA-256 출력·선택적 `OAOS_HERMES_INSTALLER_SHA256` 비교와 사용자의 `--yes`/대화 승인 흐름은 유지한다. Windows `install.ps1`은 Bash 문법 검사 대상이 아니므로 다운로드·해시·서명/출처 확인 방법과 PowerShell 실행 정책을 P3에서 공식 설치 안내에 맞춰 확정한다. 이 검사가 완성되기 전에는 다운로드한 스크립트를 자동 실행하지 않는다.

## 3. Personal 검증 계약

`bootstrap/verify/personal-verify.sh`의 현재 12개 항목과 `PASS/FAIL/MANUAL/SKIP`, `--json`, `--offline` 형식을 유지한다. OS별 서비스·자원·파일 기준만 플랫폼 계층으로 교체한다. `--offline`의 모델 SKIP은 명시적 검사 생략이며 최종 실증 PASS가 아니다. 자동 검사 PASS와 사용자 메시지·재로그온/재부팅 MANUAL 증거를 구분한다.

| 현재 항목 | 공통 통과 근거 | Linux | macOS | Windows 11 |
|---|---|---|---|---|
| 1 Hermes health · 2 Model responds | `hermes doctor`, 실제 `hermes chat -q` 응답 | 현행 검사 | 동일, timeout 대체 | 동일, CLI PATH·Git Bash 실행 확인 |
| 3 Gateway service · 4 Boot survival | `hermes gateway status`의 실제 실행·등록 정보, 재시작 후 생존 증거 | systemd 사용자 유닛 enabled/active, 필요 시 linger; 실제 재부팅 | launchd 사용자 에이전트 등록·실행 확인; 로그아웃/로그인 후 재확인 | `schtasks` ONLOGON 태스크 또는 Hermes Startup 폴백 등록 확인; 로그오프/로그온 후 재확인. 태스크 존재만으로 PASS 금지 |
| 5 Chat round-trip · 6 Allowlist | 허용 계정 응답, 비허용 계정 무응답 | MANUAL | MANUAL | MANUAL |
| 7 Wiki repository | `~/data/wiki` seed commit | Git 경로 확인 | Git 경로 확인 | Git Bash 경로·파일명 확인 |
| 8 Scheduled jobs | Hermes cron 목록의 backup·watchdog 활성 상태와 실제 실행 | 기존 cron + systemd watchdog | cron + `hermes gateway status/start` 기반 watchdog; 중복 실행 방지 | cron + Hermes 상태/시작 기반 watchdog; 외부 종료·로그온 조건 확인 |
| 9 Config files · 10 Secret hygiene | `HERMES_HOME`의 SOUL/USER/MEMORY 존재, 로그·상태 시크릿 스캔, 권한 read-back | `~/.hermes`, mode 600 | `~/.hermes`, mode 600 | `%LOCALAPPDATA%\hermes` 기본, NTFS ACL 확인; 경로 변경 존중 |
| 11 Backup works | 실제 비어 있지 않은 백업 아카이브, 오래된 검증 백업 정리 | 현행 `hermes backup`; GNU 정리 옵션 교체 | 같은 CLI, BSD 도구로 정리 확인 | 같은 CLI, MSYS 경로·잠금·ACL 확인 |
| 12 Host capacity | 대상 볼륨 여유 >10%; RAM·swap/pagefile 정책 판정 | `df -Pk`, `/proc` | `df -Pk`, macOS RAM/swap 조회 | Windows 볼륨·RAM/pagefile 조회; 경로 단위 정합 확인 |

서비스의 **등록**과 현재 프로세스 **실행**은 별도 증거다. 특히 macOS launchd와 Windows 로그온 태스크는 사용자 로그인 맥락이 필요하므로 무인 CI의 status만으로 재부팅 생존을 PASS로 만들지 않는다. Windows 네이티브 gateway는 외부에서 강제 종료됐을 때 Task Scheduler의 `RestartOnFailure`만으로 살아나지 않을 수 있다는 공식 설명을 watchdog 설계에 반영한다. [Hermes 게이트웨이 서비스 관리](https://hermes-agent.nousresearch.com/docs/user-guide/messaging), [Windows 네이티브 가이드](https://hermes-agent.nousresearch.com/docs/user-guide/windows-native)

## 4. 부트스트랩 스킬·기존 문서 갱신 계약

`skills/oaos-bootstrap/SKILL.md`를 구현 단계에서 **0.1.3**으로 올리고 Personal 세 레인을 본문에서 명시한다. 현재 이 브랜치의 버전은 0.1.1이며, 다른 브랜치의 0.1.2 변경이 합쳐지면 그 내용을 보존해 올린다. `platforms: [linux]` 메타데이터의 허용 표기와 Hermes 스킬 로더 동작을 확인한 뒤 macOS·Windows 메타데이터를 추가한다. Project·Company 경로는 Ubuntu LTS로 남긴다.

| 위치 | 구현 완료 시 변경점 |
|---|---|
| `SKILL.md` Phase 0 recon | OS 식별 후 Linux: `/etc/os-release`, `free`, `df`, `swapon`; macOS: Apple Silicon 판정, 시스템 메모리·swap·디스크 조회; Windows: **Git Bash에서 실행**하고 Windows 11/아키텍처, Git Bash·`hermes` PATH, RAM·pagefile·볼륨을 조회. OS별 정확한 명령은 P2/P3 실측 후 기재 |
| `SKILL.md` Phase 2/3/4 | OS별 prep·공식 Hermes 설치 경로·`hermes gateway` 위임·Personal 검증기 실행을 서술. Windows PowerShell은 공식 Hermes 설치와 필요한 시스템 조회/ACL 프리미티브에서만 사용 |
| `references/gates.md` | G1–G3 동일; Windows 키 저장 위치와 ACL 확인, macOS·Windows 로그인 후 게이트웨이 확인 문구. 토큰 원문 비출력 유지 |
| `references/verify-checklist.md` | 12개 공통 항목의 OS별 서비스·부팅·자원·파일 권한 판정표; 실기기 MANUAL 증거와 미실행 SKIP 구분 |
| `README.md`, `README.ko.md` | Personal 설치 표의 “host packages and systemd”를 Linux 한정으로 바꾸고 macOS/Windows 공식 설치·서비스 경로, 구현/검증 상태를 반영. 번역 쌍 동기화 |
| `docs/faq.md`, `docs/architecture-v2.0.md`, `START-HERE.md`, `editions/personal/README.md` | Linux 전용 하드웨어·apt/systemd·recon·재부팅 설명의 범위를 세 레인으로 갱신. Project·Company Ubuntu 서버 설명 유지 |

현재 브랜치 파일의 문구가 이전 OS 정합 보고서와 다를 수 있으므로 실제 파일을 기준으로 갱신한다. 위 목록은 **후속 구현 완료 시 문서 수정 범위**이며 이번 설계 작업에서는 해당 파일들을 수정하지 않는다.

## 5. 검증 전략과 열린 리스크

Linux 개발 호스트에서 `shellcheck`(변경 파일 범위), `bash -n`, OS 탐지 오버라이드를 주입한 mock 분기 테스트, `--dry-run` 무변경 테스트를 실행한다. mock은 각 플랫폼 함수의 호출·상태 전이·실패 처리를 검증하되 실 OS 기능의 증거로 쓰지 않는다. 기존 `bootstrap/tests/install-smoke.sh`와 Personal 관련 subsystem test를 통과시키고, Linux의 기존 설치·검증 결과 및 손상 상태 복구·재실행 동작에 회귀가 없어야 한다. 전체 pytest·전체 lint·verify-evidence 계열은 이 작업의 일반 완료 조건이 아니다.

**신규 인프라 제안(리뷰 포인트)**: GitHub Actions의 표준 hosted runner에서 `ubuntu-latest`/`macos-latest`/`windows-latest` 매트릭스를 추가해 각 OS의 Bash·Git Bash로 변경 파일 lint, `bash -n`, mock/dry-run을 실행한다. 공개 저장소의 표준 hosted runner 사용은 GitHub 공식 문서상 무료다. `macos-latest`의 Apple Silicon, `windows-latest`의 실제 아키텍처·이미지, Windows Git Bash와 필요한 명령의 준비 방식을 workflow에서 명시적으로 출력·검사한다. Actions는 실제 OS에서 스크립트의 실행 가능성을 확인하지만 Hermes 실설치, 게이트웨이 등록, Telegram/LLM 왕복, 재부팅을 대체하지 않는다. [GitHub hosted runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners), [GitHub Actions 과금](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

실기기에서 Ubuntu·Apple Silicon Mac·Windows 11(x86_64, 가능하면 aarch64)의 공식 Hermes 설치 → OAOS 전체 설치 → 상태 재개 → 키·토큰 게이트 → 모델·Telegram 왕복 → 권한·백업 → 로그아웃/로그온·안전한 재부팅 후 gateway 및 cron을 읽어 검증한다. Windows 11 PC 사용 가능 여부, macOS 기기 가용성, Windows ARM64 기기, GUI/사용자 로그인 세션 및 Telegram 비허용 계정은 **확인 필요**다. 확보되지 않은 조합은 CI PASS와 별개로 **미검증**으로 보고한다. 시간대 ID 매핑, Windows ACL 및 경로, macOS sleep 정책, Hermes Desktop과 MSIX의 CLI/Git Bash 통합 역시 실측 전까지 **확인 필요**다.

## 6. 단계별 구현 계획

| 단계 | 산출물 | 테스트 | 완료 조건 |
|---|---|---|---|
| P1 공통 platform 계층·무회귀 리팩터 | `platform.sh`, Linux 프리미티브, Personal 설치기·검증기의 직접 OS 호출 분리, 상태 호환 처리 | 변경 파일 `shellcheck`·`bash -n`; 기존 Personal smoke/subsystem; mock·dry-run | Linux 단계·게이트·종료 코드·기존 `done` 재개·검증 12항목 무회귀 |
| P2 macOS 레인 | Apple Silicon 탐지, 공식 설치 연결, launchd·자원·시간대·권한·cron 경로, 스킬 초안 | macOS Bash 3.2 syntax·dry-run·subsystem; 가능한 실기기 설치 | macOS 자동 read-back PASS, 수동 로그인·메시지·재부팅 증거가 없으면 미검증 표시 |
| P3 Windows 레인 | Git Bash 탐지, `install.ps1` 연결, `HERMES_HOME`/MSYS 경로·ACL·schtasks/폴백·자원·cron 경로 | Windows Git Bash syntax·dry-run·subsystem; Windows 11 PC 실증 | 네이티브 설치·상태 재개·ACL·로그온 생존 PASS; x86_64/aarch64 별도 결과 기록 |
| P4 CI | GitHub Actions OS 매트릭스 workflow(도입 리뷰 후) | 3 OS lint + dry-run + mock | runner OS/아키텍처와 각 검사 결과 공개; 기존 Linux 테스트 계속 통과 |
| P5 실기기 검증·문서 정합 | 실기기 결과 보고서, 스킬 0.1.3, README/FAQ/architecture/START-HERE 갱신 | 실제 LLM·Telegram·백업·로그온/재부팅 read-back | 검증한 OS/아키텍처만 구현 완료 표기; 미실측 조합은 확인 필요 유지; Linux 무회귀 최종 확인 |

P2/P3 구현 순서는 의존성을 뜻하지 않으나 P1 계약을 공통으로 사용한다. CI 신규 인프라 도입 여부는 P4 착수 리뷰에서 결정한다. P5의 실기기 가용성과 증거 범위를 확보하기 전에는 “세 OS 검증 완료”라고 표기하지 않는다.
