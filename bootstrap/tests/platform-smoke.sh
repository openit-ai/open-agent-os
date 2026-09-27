#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"
# shellcheck source=bootstrap/lib/platform.sh
. "$repo_root/bootstrap/lib/platform.sh"

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
test_sha256() {
  python3 - "$1" <<'PY'
import hashlib, pathlib, sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
}

# Every mocked lane starts from an explicit environment, even inside Hermes.
unset HERMES_HOME OAOS_PLATFORM OAOS_TEST_PLATFORM OAOS_TEST_MODE MSYSTEM LOCALAPPDATA APPDATA
unset OAOS_DRY_RUN OAOS_NO_LOG_FILE OAOS_STATE_EDITION OAOS_STAGES

# uname is mocked only inside this subshell; no host service or package is touched.
(
  # shellcheck disable=SC2317
  uname() { case $1 in -s) printf '%s\n' "$fake_kernel" ;; -m) printf '%s\n' "$fake_arch" ;; esac; }
  fake_kernel=Linux fake_arch=x86_64
  [[ $(os_detect) == linux ]] || fail 'Linux detection'
  fake_kernel=Darwin fake_arch=arm64
  [[ $(os_detect) == macos ]] || fail 'Apple Silicon detection'
  fake_arch=x86_64
  if os_detect >/dev/null 2>&1; then fail 'Intel Mac accepted'; fi
  fake_kernel=MINGW64_NT fake_arch=x86_64
  unset MSYSTEM
  [[ $(os_detect) == windows-gitbash ]] || fail 'direct Git Bash detection without MSYSTEM'
  MSYSTEM=MINGW64; export MSYSTEM
  [[ $(os_detect) == windows-gitbash ]] || fail 'Git Bash detection with MSYSTEM'
  MSYSTEM=OTHER
  [[ $(os_detect) == windows-gitbash ]] || fail 'Git Bash detection with unrelated MSYSTEM'
  unset MSYSTEM
  fake_kernel=CYGWIN_NT
  [[ $(os_detect) == windows-gitbash ]] || fail 'CYGWIN kernel detection'
  fake_kernel=MINGW64_NT
  unset BASH_VERSION
  if os_detect >/dev/null 2>&1; then fail 'ash/BusyBox-shaped shell accepted'; fi
  fake_kernel=FreeBSD fake_arch=x86_64
  if os_detect >/dev/null 2>&1; then fail 'unsupported kernel accepted'; fi
)
printf 'PASS: actual kernel/architecture/Git Bash lane rules with mocked uname\n'

HOME="$temp_root/home"; export HOME
mkdir -p "$HOME"
chmod 700 "$HOME"
OAOS_TEST_PLATFORM=macos
export OAOS_TEST_PLATFORM
unset OAOS_TEST_MODE
if os_detect >/dev/null 2>&1; then fail 'override accepted without test mode'; fi
OAOS_TEST_MODE=1; export OAOS_TEST_MODE
for OAOS_TEST_PLATFORM in macos windows-gitbash; do
  export OAOS_TEST_PLATFORM
  os_detect >/dev/null || fail 'test override rejected'
  code=0
  HOME="$HOME" OAOS_TEST_MODE=1 OAOS_TEST_PLATFORM="$OAOS_TEST_PLATFORM" \
    bash "$repo_root/editions/personal/install.sh" --dry-run --stage prep >"$temp_root/entry.out" 2>"$temp_root/entry.err" || code=$?
  if [[ $code != 0 ]] || ! grep -Fq 'Would run prep' "$temp_root/entry.out"; then fail 'cross-platform dry-run failed'; fi
done
[[ ! -e $HOME/.oaos && ! -e $HOME/.oaos-install ]] || fail 'dry-run entry changed HOME'
printf 'PASS: test-only overrides and macOS/Windows dry-run without side effects\n'

(
  unset HERMES_HOME OAOS_TEST_PLATFORM OAOS_TEST_MODE MSYSTEM LOCALAPPDATA APPDATA
  OAOS_PLATFORM=macos
  export OAOS_PLATFORM
  [[ $(platform_hermes_home) == "$HOME/.hermes" ]] || fail 'macOS default Hermes path'
  HERMES_HOME="$HOME/custom hermes"; export HERMES_HOME
  [[ $(platform_hermes_home) == "$HERMES_HOME" ]] || fail 'macOS HERMES_HOME precedence'
  # shellcheck disable=SC2317,SC2329 # Called through the platform dispatcher.
  sysctl() {
    case $* in '-n hw.memsize') printf '17179869184\n' ;; 'vm.swapusage') printf 'vm.swapusage: total = 1024.00M  used = 0.00M  free = 1024.00M\n' ;; *) return 1 ;; esac
  }
  # shellcheck disable=SC2317,SC2329
  shasum() { [[ $1 == -a && $2 == 256 ]] || return 1; printf '%s  %s\n' "$(test_sha256 "$3")" "$3"; }
  mock_mac_mode=644
  # shellcheck disable=SC2317
  chmod() {
    if [[ $(uname -s) == MINGW* || $(uname -s) == MSYS* ]]; then mock_mac_mode=$2; fi
    command chmod "$@"
  }
  # shellcheck disable=SC2317,SC2329
  stat() {
    if [[ $1 == -f && $2 == %Lp ]]; then
      if [[ $(uname -s) == Darwin ]]; then command stat -f '%Lp' "$3"
      elif [[ $(uname -s) == MINGW* || $(uname -s) == MSYS* ]]; then printf '%s\n' "$mock_mac_mode"
      else command stat -c '%a' "$3"; fi
    elif [[ $1 == -f && $2 == %u ]]; then id -u
    else command stat "$@"; fi
  }
  # shellcheck disable=SC2317,SC2329
  systemsetup() { [[ $1 == -gettimezone ]] && printf 'Time Zone: Asia/Seoul\n'; }
  # shellcheck disable=SC2317,SC2329
  readlink() { [[ $1 == /etc/localtime ]] && printf '/var/db/timezone/zoneinfo/Asia/Seoul\n'; }
  # shellcheck disable=SC2317,SC2329
  launchctl() { printf '123 0 com.hermes.gateway\n'; }
  # shellcheck disable=SC2317,SC2329
  hermes() { [[ $1 == gateway && $2 == status ]] && printf 'Status: running\n'; }
  [[ $(platform_memory_kib) == 16777216 && $(platform_swap_state) == 1 ]] || fail 'macOS resource units'
  [[ $(platform_timezone_get) == Asia/Seoul ]] || fail 'macOS timezone read'
  [[ $(platform_path scripts) == "$HERMES_HOME/scripts" ]] || fail 'macOS Hermes path override'
  OAOS_DRY_RUN=1; export OAOS_DRY_RUN
  printf 'secret\n' > "$HOME/mac-secret"
  chmod 644 "$HOME/mac-secret"
  platform_secret_protect "$HOME/mac-secret" >/dev/null
  [[ $(platform_file_mode "$HOME/mac-secret") == 644 ]] || fail 'macOS dry-run changed secret'
  OAOS_DRY_RUN=0; export OAOS_DRY_RUN
  platform_secret_protect "$HOME/mac-secret" || fail 'macOS secret protection'
  platform_secret_check "$HOME/mac-secret" || fail 'macOS secret read-back'
  [[ $(platform_sha256 "$HOME/mac-secret") == $(test_sha256 "$HOME/mac-secret") ]] || fail 'macOS shasum'
  platform_gateway_autostart check || fail 'macOS launchd diagnostic'
  code=0
  platform_timezone_set Asia/Tokyo 2>"$temp_root/mac-block.err" || code=$?
  if [[ $code != 3 ]] || ! grep -Fq BLOCKED "$temp_root/mac-block.err"; then fail 'macOS timezone was not blocked'; fi
  # shellcheck disable=SC2317,SC2329
  git() { return 1; }
  code=0
  platform_require_tools >/dev/null 2>"$temp_root/mac-prereq.err" || code=$?
  if [[ $code != 3 ]] || ! grep -Fq 'install git manually' "$temp_root/mac-prereq.err"; then fail 'macOS missing git was not blocked'; fi
)
printf 'PASS: mocked macOS resources, shasum, secret owner/mode, paths, launchd, and timezone BLOCKED\n'

(
  unset HERMES_HOME OAOS_TEST_PLATFORM OAOS_TEST_MODE
  OAOS_PLATFORM=windows-gitbash
  export OAOS_PLATFORM
  MSYSTEM=MINGW64; export MSYSTEM
  LOCALAPPDATA='C:\Users\Test\AppData\Local'; APPDATA='C:\Users\Test\AppData\Roaming'
  export LOCALAPPDATA APPDATA
  # shellcheck disable=SC2317
  cygpath() {
    case $1 in
      -u) if [[ $2 == 'D:\Data\Hermes' ]]; then printf '%s/other-hermes\n' "$HOME"; else printf '%s/local/hermes\n' "$HOME"; fi ;;
      -w) printf 'C:\\mock\\%s\n' "${2##*/}" ;;
      *) return 1 ;;
    esac
  }
  # shellcheck disable=SC2317,SC2329
  powershell.exe() {
    if [[ $* == *' -File '* ]]; then printf '%s\n' "$*" > "$temp_root/ps-installer.args"; return 0; fi
    case $* in
      *TotalPhysicalMemory*) printf '16777216\r\n' ;;
      *Win32_PageFileUsage*) printf '4096\r\n' ;;
      *Get-TimeZone*) printf 'Korea Standard Time\r\n' ;;
      *Get-Acl*) [[ ${mock_acl_fail:-0} == 0 ]] ;;
      *WindowsIdentity*User.Value*) printf 'S-1-5-21-1000\r\n' ;;
      *) return 1 ;;
    esac
  }
  # shellcheck disable=SC2317,SC2329
  schtasks.exe() { [[ $* == *'/XML'* ]] && printf '<Task><Triggers><LogonTrigger></LogonTrigger></Triggers></Task>\n'; }
  # shellcheck disable=SC2317,SC2329
  icacls.exe() { printf '%s\n' "$*" > "$temp_root/icacls.args"; }
  # shellcheck disable=SC2317,SC2329
  sha256sum() { printf '%s  %s\n' "$(test_sha256 "$1")" "$1"; }
  # shellcheck disable=SC2317,SC2329
  hermes() {
    [[ $1 == gateway ]] || return 1
    case $2 in status) printf 'Hermes_Gateway Status: running\n' ;; install) printf 'install\n' > "$temp_root/gateway-install" ;; esac
  }
  [[ $(platform_hermes_home) == "$HOME/local/hermes" ]] || fail 'Windows LOCALAPPDATA conversion'
  [[ $(platform_path scripts) == "$HOME/local/hermes/scripts" && $(platform_path state) == "$HOME/.oaos-install/state.json" ]] || fail 'Windows platform paths'
  HERMES_HOME='D:\Data\Hermes'; export HERMES_HOME
  [[ $(platform_hermes_home) == "$HOME/other-hermes" ]] || fail 'Windows HERMES_HOME override conversion'
  unset HERMES_HOME
  [[ $(platform_memory_kib) == 16777216 && $(platform_swap_state) == 1 ]] || fail 'Windows CIM units'
  [[ $(platform_timezone_get) == 'Korea Standard Time' ]] || fail 'Windows timezone ID'
  printf 'installer\n' > "$HOME/install.ps1"
  [[ $(platform_sha256 "$HOME/install.ps1") == $(test_sha256 "$HOME/install.ps1") ]] || fail 'Windows SHA-256 tool selection'
  expected_sha=$(test_sha256 "$HOME/install.ps1")
  (
    # shellcheck disable=SC2317,SC2329
    sha256sum() { return 1; }
    # shellcheck disable=SC2317,SC2329
    shasum() { return 1; }
    # shellcheck disable=SC2317,SC2329
    certutil.exe() { printf 'SHA256 hash of file:\r\n%s\r\nCertUtil: command completed successfully.\r\n' "$expected_sha"; }
    [[ $(platform_sha256 "$HOME/install.ps1") == "$expected_sha" ]] || fail 'Windows certutil SHA-256 fallback'
  )
  platform_install_hermes "$HOME/install.ps1" || fail 'Windows PowerShell installer invocation'
  grep -Fq -- '-ExecutionPolicy Bypass -File C:\mock\install.ps1' "$temp_root/ps-installer.args" || fail 'Windows installer path or invocation'
  platform_gateway_autostart check || fail 'Windows ONLOGON registration'
  OAOS_DRY_RUN=1; export OAOS_DRY_RUN
  platform_gateway_install >/dev/null
  [[ ! -e $temp_root/gateway-install ]] || fail 'Windows dry-run installed gateway'
  OAOS_DRY_RUN=0; export OAOS_DRY_RUN
  platform_gateway_install || fail 'Windows Hermes gateway install'
  [[ -e $temp_root/gateway-install ]] || fail 'Windows gateway install not called'
  printf 'secret\n' > "$HOME/win-secret"
  platform_secret_protect "$HOME/win-secret" || fail 'Windows ACL protection'
  grep -Fq '/inheritance:r' "$temp_root/icacls.args" || fail 'Windows icacls not invoked'
  printf 'replacement\n' | platform_atomic_write "$HOME/win-secret" || fail 'Windows atomic ACL write'
  [[ $(cat "$HOME/win-secret") == replacement ]] || fail 'Windows atomic content'
  mock_acl_fail=1
  if platform_secret_check "$HOME/win-secret"; then fail 'Windows ACL failure accepted'; fi
  code=0
  platform_timezone_set Asia/Seoul 2>"$temp_root/win-block.err" || code=$?
  if [[ $code != 3 ]] || ! grep -Fq BLOCKED "$temp_root/win-block.err"; then fail 'Windows unmapped timezone was not blocked'; fi
  # shellcheck disable=SC2317,SC2329
  python3() { return 1; }
  # shellcheck disable=SC2317,SC2329
  python() { return 1; }
  code=0
  platform_python >/dev/null 2>"$temp_root/python-block.err" || code=$?
  [[ $code == 3 ]] || fail 'Windows missing Python was not blocked'
  # shellcheck disable=SC2317,SC2329
  git() { return 1; }
  code=0
  platform_require_tools >/dev/null 2>"$temp_root/git-block.err" || code=$?
  if [[ $code != 3 ]] || ! grep -Fq 'missing git' "$temp_root/git-block.err"; then fail 'Windows missing git was not blocked'; fi
)
printf 'PASS: mocked Windows CIM, timezone, path, SHA, PowerShell install, ONLOGON, ACL, and BLOCKED paths\n'

# Generate macOS cron scripts through the real entrypoint; Hermes is a local mock.
mkdir -p "$temp_root/cron-bin" "$temp_root/cron-home"
cat > "$temp_root/cron-bin/hermes" <<'SH'
#!/usr/bin/env bash
[[ $1 == cron ]] || exit 1
case $2 in
  list) [[ ! -f $HERMES_MOCK_JOBS ]] || cat "$HERMES_MOCK_JOBS" ;;
  create)
    shift 2
    while (($#)); do
      if [[ $1 == --name ]]; then printf '%s\n' "$2" >> "$HERMES_MOCK_JOBS"; exit 0; fi
      shift
    done
    exit 1 ;;
esac
SH
chmod 700 "$temp_root/cron-bin/hermes"
HOME="$temp_root/cron-home" HERMES_HOME="$temp_root/custom hermes" HERMES_MOCK_JOBS="$temp_root/jobs" \
  PATH="$temp_root/cron-bin:$PATH" OAOS_TEST_MODE=1 OAOS_TEST_PLATFORM=macos \
  bash "$repo_root/editions/personal/install.sh" --stage cron >"$temp_root/cron.out" 2>"$temp_root/cron.err" || fail 'macOS cron generation'
backup_script="$temp_root/custom hermes/scripts/oaos-daily-backup.sh"
watchdog_script="$temp_root/custom hermes/scripts/oaos-gateway-watchdog.sh"
[[ -f $backup_script && -f $watchdog_script ]] || fail 'macOS cron scripts absent'
grep -Fq 'custom\ hermes' "$backup_script" || fail 'backup script ignored custom Hermes path'
grep -Fq 'hermes gateway start' "$watchdog_script" || fail 'watchdog start missing'
bash -n "$backup_script" "$watchdog_script" || fail 'generated scripts syntax'
printf 'PASS: generated macOS backup and watchdog scripts honor HERMES_HOME in isolated HOME\n'

OAOS_TEST_PLATFORM=linux; export OAOS_TEST_PLATFORM
unset HERMES_HOME MSYSTEM LOCALAPPDATA APPDATA
os_detect >/dev/null
OAOS_DRY_RUN=1 OAOS_NO_LOG_FILE=1
export OAOS_DRY_RUN OAOS_NO_LOG_FILE
printf 'original\n' > "$HOME/private"
chmod 644 "$HOME/private"
printf 'replacement\n' | platform_atomic_write "$HOME/private" >/dev/null
platform_secret_protect "$HOME/private" >/dev/null
platform_timezone_set UTC >/dev/null
platform_sleep_policy >/dev/null
platform_gateway_install >/dev/null
platform_install_hermes "$HOME/missing-installer" >/dev/null
[[ $(cat "$HOME/private") == original ]] || fail 'dry-run changed file'
if [[ $(uname -s) == Linux ]]; then
  [[ $(platform_file_mode "$HOME/private") == 644 ]] || fail 'dry-run changed file mode'
fi
[[ ! -e $HOME/private.tmp && ! -e $HOME/.oaos ]] || fail 'dry-run created files'
printf 'PASS: platform mutators honor dry-run without file or service changes\n'

OAOS_DRY_RUN=0; export OAOS_DRY_RUN
if [[ $(uname -s) == Linux ]]; then
  printf 'replacement\n' | platform_atomic_write "$HOME/private"
  if [[ $(cat "$HOME/private") != replacement ]] || ! platform_secret_check "$HOME/private"; then fail 'atomic write or secret mode'; fi
  chmod 777 "$HOME"
  if platform_secret_check "$HOME/private"; then fail 'writable secret parent accepted'; fi
  chmod 700 "$HOME"
  platform_secret_check "$HOME/private" || fail 'private parent rejected'
  [[ $(platform_sha256 "$HOME/private") == $(test_sha256 "$HOME/private") ]] || fail 'SHA-256 value'
  [[ $(platform_hermes_home) == "$HOME/.hermes" && $(platform_path state) == "$HOME/.oaos-install/state.json" ]] || fail 'Linux paths'
  stage_mark wiki applied 'test'
  if [[ $(stage_status wiki) != applied ]] || stage_done wiki; then fail 'applied status'; fi
  stage_mark wiki verified 'test'
  if [[ $(stage_status wiki) != verified ]] || stage_done wiki; then fail 'verified status'; fi
  stage_mark optional SKIP 'explicit optional item'
  if [[ $(stage_status optional) != SKIP ]] || stage_done optional; then fail 'SKIP status'; fi
  if (stage_mark wiki SKIP 'forbidden') >/dev/null 2>&1; then fail 'required stage accepted SKIP'; fi
  stage_mark wiki 'done' 'legacy'
  stage_done wiki || fail 'legacy done resume'
  printf 'PASS: Linux hash, atomic mode, paths, and legacy/new status read-back\n'
else
  printf 'SKIP: Linux native hash/mode/state checks require Linux host primitives\n'
fi

backup_dir="$HOME/backups with spaces"
mkdir -p "$backup_dir"
python3 - "$backup_dir" <<'PY'
import os, pathlib, sys
root = pathlib.Path(sys.argv[1])
for i, name in enumerate(('hermes-verify-a.zip', 'hermes-verify-\nline.zip', 'hermes-verify-c.zip')):
    path = root / name
    path.write_bytes(b'x')
    os.utime(path, (100 + i, 100 + i))
PY
platform_backup_prune "$backup_dir"
[[ ! -e $backup_dir/hermes-verify-a.zip && -e $backup_dir/hermes-verify-c.zip && -e $backup_dir/$'hermes-verify-\nline.zip' ]] || fail 'portable backup pruning'
printf 'PASS: backup pruning handles spaces and newlines\n'
