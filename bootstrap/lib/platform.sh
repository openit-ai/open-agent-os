#!/usr/bin/env bash
# Personal platform boundary. Keep syntax compatible with macOS Bash 3.2.

platform_blocked() {
  printf 'BLOCKED: %s is unavailable for %s.\n' "$1" "${OAOS_PLATFORM:-unknown}" >&2
  return 3
}

platform_arch() {
  case $(uname -m) in
    x86_64|amd64) printf 'x86_64\n' ;;
    aarch64|arm64) printf 'arm64\n' ;;
    *) printf 'Unsupported CPU architecture: %s\n' "$(uname -m)" >&2; return 3 ;;
  esac
}

os_detect() {
  local kernel arch lane
  kernel=$(uname -s)
  arch=$(platform_arch) || return 3
  case $kernel in
    Linux) lane=linux ;;
    Darwin) lane=macos ;;
    MINGW*|MSYS*|CYGWIN*) lane=windows-gitbash ;;
    *) printf 'BLOCKED: unsupported OS: %s.\n' "$kernel" >&2; return 3 ;;
  esac
  if [[ $lane == macos && $arch != arm64 ]]; then
    printf 'BLOCKED: Personal macOS requires Apple Silicon.\n' >&2; return 3
  fi
  if [[ $lane == windows-gitbash && ( -z ${BASH_VERSION:-} || -z ${BASH_VERSINFO[0]:-} ) ]]; then
    printf 'BLOCKED: Windows requires real Git Bash; ash/BusyBox is unsupported.\n' >&2; return 3
  fi
  if [[ -n ${OAOS_TEST_PLATFORM:-} ]]; then
    if [[ ${OAOS_TEST_MODE:-0} != 1 ]]; then
      printf 'BLOCKED: OAOS_TEST_PLATFORM requires OAOS_TEST_MODE=1.\n' >&2; return 3
    fi
    case $OAOS_TEST_PLATFORM in linux|macos|windows-gitbash) lane=$OAOS_TEST_PLATFORM ;;
      *) printf 'BLOCKED: invalid OAOS_TEST_PLATFORM.\n' >&2; return 3 ;; esac
  fi
  OAOS_PLATFORM=$lane
  export OAOS_PLATFORM
  printf '%s\n' "$lane"
}

platform_linux() { [[ ${OAOS_PLATFORM:-} == linux ]] || platform_blocked "$1"; }

platform_linux_require_tools() {
  platform_linux platform_require_tools || return 3
  local tool
  for tool in bash git curl python3 tar mktemp awk grep sed; do
    command -v "$tool" >/dev/null 2>&1 || { printf 'BLOCKED: required tool unavailable: %s\n' "$tool" >&2; return 3; }
  done
}

platform_linux_check_os() {
  platform_linux platform_check_os || return 3
  local id='' version=''
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    id=${ID:-}
    version=${VERSION_ID:-}
  fi
  if [[ $id != ubuntu || ! $version =~ ^(20\.04|22\.04|24\.04|26\.04)$ ]]; then
    warn "Ubuntu LTS is recommended; detected ${id:-unknown} ${version:-unknown}. Continuing."
  fi
}

platform_linux_hermes_home() { platform_linux platform_hermes_home || return 3; printf '%s/.hermes\n' "${HOME:?}"; }
platform_linux_oaos_home() { platform_linux platform_oaos_home || return 3; printf '%s\n' "${HOME:?}"; }
platform_linux_path() {
  platform_linux platform_path || return 3
  case $1 in
    state) printf '%s/.oaos-install/state.json\n' "$HOME" ;;
    logs) printf '%s/.oaos/logs\n' "$HOME" ;;
    backups) printf '%s/.oaos/backups\n' "$HOME" ;;
    cache) printf '%s/.oaos/cache\n' "$HOME" ;;
    wiki) printf '%s/data/wiki\n' "$HOME" ;;
    scripts) printf '%s/.hermes/scripts\n' "$HOME" ;;
    *) printf 'Unknown platform path: %s\n' "$1" >&2; return 1 ;;
  esac
}

# Prints missing Ubuntu package names, one per line. Inspection has no side effects.
platform_linux_package_prereqs() {
  platform_linux platform_package_prereqs || return 3
  local pkg
  for pkg in git curl xz-utils ca-certificates; do
    if ! command -v dpkg-query >/dev/null 2>&1 ||
       ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
      printf '%s\n' "$pkg"
    fi
  done
}

platform_linux_package_install() {
  platform_linux platform_package_install || return 3
  (($#)) || return 0
  if command -v apt-get >/dev/null 2>&1; then
    run_root apt-get update && run_root apt-get install -y "$@"
  else
    warn 'Package manager unavailable; required packages need manual installation.'
    return 3
  fi
}

platform_linux_install_hermes() {
  platform_linux platform_install_hermes || return 3
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info 'Would run saved official Hermes installer.'; return 0; fi
  bash "$1"
}

platform_linux_memory_kib() { platform_linux platform_memory_kib || return 3; awk '/^MemTotal:/ {print $2}' /proc/meminfo; }
platform_linux_swap_state() { platform_linux platform_swap_state || return 3; awk 'END {print NR-1}' /proc/swaps; }
platform_linux_disk_kib() { platform_linux platform_disk_kib || return 3; df -Pk "$1" | awk 'NR==2 {print $2, $4}'; }
platform_linux_timezone_get() { platform_linux platform_timezone_get || return 3; timedatectl show -p Timezone --value; }
platform_linux_timezone_set() {
  platform_linux platform_timezone_set || return 3
  run_root timedatectl set-timezone "$1"
}

platform_linux_swap_prepare() {
  platform_linux platform_swap_prepare || return 3
  local ram_kb swap_count
  ram_kb=$(platform_memory_kib) || return 3
  swap_count=$(platform_swap_state) || return 3
  if ((ram_kb < 16777216 && swap_count == 0)); then
    if [[ -e /swapfile ]]; then warn 'An inactive /swapfile exists; preserving it for manual review.'; return 3; fi
    run_root fallocate -l 8G /swapfile && run_root chmod 600 /swapfile &&
      run_root mkswap /swapfile && run_root swapon /swapfile || return 3
    run_root bash -c 'grep -qE "^/swapfile[[:space:]]" /etc/fstab || printf "%s\n" "/swapfile none swap sw 0 0" >> /etc/fstab'
  fi
}

platform_linux_sleep_policy() {
  platform_linux platform_sleep_policy || return 3
  local blocked=0
  if [[ ! -f /etc/systemd/logind.conf.d/oaos-personal.conf ]]; then
    if run_root install -d -m 755 /etc/systemd/logind.conf.d; then
      printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nIdleAction=ignore\n' |
        run_root tee /etc/systemd/logind.conf.d/oaos-personal.conf >/dev/null || blocked=1
      warn 'The logind drop-in takes effect after a safe logind restart or reboot.'
    else blocked=1; fi
  fi
  run_root systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target || blocked=1
  ((blocked == 0))
}

platform_linux_file_mode() { platform_linux platform_file_mode || return 3; stat -c '%a' "$1"; }
platform_linux_secret_protect() {
  platform_linux platform_secret_protect || return 3
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info "Would protect $1."; return 0; fi
  chmod 600 "$1"
}
platform_linux_secret_check() {
  platform_linux platform_secret_check || return 3
  local parent mode
  [[ -f $1 && $(platform_file_mode "$1") == 600 ]] || return 1
  parent=$(dirname "$1")
  [[ -d $parent && -x $parent ]] || return 1
  mode=$(platform_file_mode "$parent") || return 1
  [[ $mode =~ ^[0-7]{3,4}$ ]] || return 1
  (( (8#$mode & 0022) == 0 ))
}
platform_linux_sha256() { platform_linux platform_sha256 || return 3; sha256sum "$1" | awk '{print $1}'; }
platform_linux_atomic_write() {
  platform_linux platform_atomic_write || return 3
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info "Would atomically write $1."; return 0; fi
  local tmp
  tmp=$(mktemp "$1.tmp.XXXXXX") || return 1
  if cat > "$tmp" && platform_secret_protect "$tmp" && mv -f -- "$tmp" "$1"; then return 0; fi
  rm -f -- "$tmp"
  return 1
}

platform_linux_gateway_install() {
  platform_linux platform_gateway_install || return 3
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info 'Would install Hermes gateway.'; return 0; fi
  hermes gateway install --start-on-login --start-now >/dev/null
}
platform_linux_gateway_status() {
  platform_linux platform_gateway_status || return 3
  [[ -f $HOME/.config/systemd/user/hermes-gateway.service ]] || return 1
  hermes gateway status 2>/dev/null | grep -Eiq 'running|active'
}
platform_linux_gateway_service_present() {
  platform_linux platform_gateway_service_present || return 3
  [[ -f $HOME/.config/systemd/user/hermes-gateway.service ]]
}
platform_linux_gateway_autostart() {
  platform_linux platform_gateway_autostart || return 3
  local action=${1:-check}
  if [[ $action == registered ]]; then
    systemctl --user is-enabled hermes-gateway >/dev/null 2>&1
    return
  fi
  if [[ $action == ensure ]]; then
    systemctl --user is-enabled hermes-gateway >/dev/null 2>&1 || { warn 'Gateway user service is not enabled.'; return 3; }
    if [[ $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true) != yes ]]; then
      run_root loginctl enable-linger "$(id -un)" || return 3
    fi
    return 0
  fi
  [[ $action == check ]] || return 1
  [[ -L $HOME/.config/systemd/user/default.target.wants/hermes-gateway.service ]] &&
    [[ $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true) == yes ]]
}

platform_linux_chat_probe() {
  platform_linux platform_chat_probe || return 3
  python3 - <<'PY'
import subprocess
try:
    result = subprocess.run(['hermes', 'chat', '-q', 'Reply with exactly: OK'],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=30, check=False)
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit(1)
raise SystemExit(0 if b'OK' in result.stdout else 1)
PY
}

platform_linux_backup_prune() {
  platform_linux platform_backup_prune || return 3
  [[ ${OAOS_DRY_RUN:-0} != 1 ]] || { info 'Would prune old verify backups.'; return 0; }
  python3 - "$1" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
files = sorted((p for p in root.glob('hermes-verify-*.zip') if p.is_file()),
               key=lambda p: p.stat().st_mtime, reverse=True)
for path in files[2:]:
    path.unlink()
PY
}

# Windows path conversion is confined here. All other functions use MSYS paths.
platform_windows_convert_path() {
  command -v cygpath >/dev/null 2>&1 || { platform_blocked cygpath; return 3; }
  case $1 in
    to-posix) cygpath -u "$2" ;;
    to-native) cygpath -w "$2" ;;
    *) return 1 ;;
  esac
}

platform_python() {
  local candidate
  if [[ ${OAOS_PLATFORM:-} == windows-gitbash ]]; then
    for candidate in python3 python; do
      if command -v "$candidate" >/dev/null 2>&1 &&
         "$candidate" -c 'import sys; assert sys.version_info[0] == 3' >/dev/null 2>&1; then
        command -v "$candidate"
        return 0
      fi
    done
    if [[ -n ${LOCALAPPDATA:-} ]]; then
      candidate=$(platform_windows_convert_path to-posix "$LOCALAPPDATA/hermes/hermes-agent/venv/Scripts/python.exe") || return 3
      if [[ -x $candidate ]] && "$candidate" -c 'import sys; assert sys.version_info[0] == 3' >/dev/null 2>&1; then
        printf '%s\n' "$candidate"
        return 0
      fi
    fi
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  printf 'BLOCKED: Python 3 is required for state JSON, Telegram checks, model timeout, backup pruning, and secret scanning.\n' >&2
  return 3
}

platform_macos_require_tools() {
  local tool missing=0
  for tool in bash git curl; do
    if ! command -v "$tool" >/dev/null 2>&1 || ! "$tool" --version >/dev/null 2>&1; then
      printf 'BLOCKED: install %s manually (Xcode Command Line Tools or your package manager); no automatic brew installation.\n' "$tool" >&2
      missing=1
    fi
  done
  for tool in tar mktemp awk grep sed shasum sysctl; do
    command -v "$tool" >/dev/null 2>&1 || { printf 'BLOCKED: required macOS tool unavailable: %s\n' "$tool" >&2; missing=1; }
  done
  platform_python >/dev/null || missing=1
  ((missing == 0)) || return 3
}
platform_macos_check_os() { :; }
platform_macos_hermes_home() { printf '%s\n' "${HERMES_HOME:-$HOME/.hermes}"; }
platform_macos_oaos_home() { printf '%s\n' "$HOME"; }
platform_macos_path() {
  case $1 in
    state) printf '%s/.oaos-install/state.json\n' "$HOME" ;;
    logs) printf '%s/.oaos/logs\n' "$HOME" ;;
    backups) printf '%s/.oaos/backups\n' "$HOME" ;;
    cache) printf '%s/.oaos/cache\n' "$HOME" ;;
    wiki) printf '%s/data/wiki\n' "$HOME" ;;
    scripts) printf '%s/scripts\n' "$(platform_hermes_home)" ;;
    *) return 1 ;;
  esac
}
platform_macos_package_prereqs() { :; }
platform_macos_package_install() { platform_blocked 'manual prerequisite installation'; }
platform_macos_install_hermes() {
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info 'Would run saved official Hermes installer.'; return 0; fi
  HERMES_HOME=$(platform_hermes_home) bash "$1"
}
platform_macos_memory_kib() {
  local bytes
  bytes=$(sysctl -n hw.memsize) || return 3
  [[ $bytes =~ ^[0-9]+$ ]] || return 3
  printf '%s\n' "$((bytes / 1024))"
}
platform_macos_swap_state() {
  local value
  value=$(sysctl vm.swapusage) || return 3
  # Capacity check needs to know whether OS-managed swap exists, not its usage.
  if [[ $value =~ total[[:space:]]*=[[:space:]]*([0-9]+([.][0-9]+)?)M ]]; then
    awk -v amount="${BASH_REMATCH[1]}" 'BEGIN {print (amount + 0 > 0 ? 1 : 0)}'
  else return 3; fi
}
platform_macos_disk_kib() { df -Pk "$1" | awk 'NR==2 {print $2, $4}'; }
platform_macos_timezone_get() {
  local zone
  zone=$(readlink /etc/localtime 2>/dev/null || true)
  if [[ $zone == */zoneinfo/* ]]; then printf '%s\n' "${zone##*/zoneinfo/}"; return 0; fi
  zone=$(systemsetup -gettimezone 2>/dev/null) || { platform_blocked 'macOS timezone read'; return 3; }
  printf '%s\n' "${zone#Time Zone: }"
}
platform_macos_timezone_set() {
  printf 'BLOCKED: set macOS timezone manually in System Settings > General > Date & Time (requested %s); administrator access and systemsetup behavior require device validation.\n' "$1" >&2
  return 3
}
platform_macos_swap_prepare() { info 'macOS manages swap; inspect virtual memory in System Settings if capacity is insufficient.'; }
platform_macos_sleep_policy() { info 'Confirm macOS sleep and login behavior manually for gateway availability.'; }
platform_macos_file_mode() { stat -f '%Lp' "$1"; }
platform_macos_secret_protect() {
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info "Would protect $1."; return 0; fi
  chmod 600 "$1" && platform_macos_secret_check "$1"
}
platform_macos_secret_check() {
  [[ -f $1 && $(platform_macos_file_mode "$1") == 600 ]] || return 1
  [[ $(stat -f '%u' "$1") == $(id -u) ]] || return 1
  [[ -x $(dirname "$1") ]]
}
platform_macos_sha256() { shasum -a 256 "$1" | awk '{print $1}'; }
platform_macos_atomic_write() { platform_portable_atomic_write "$1"; }
platform_macos_gateway_install() {
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info 'Would install Hermes gateway.'; return 0; fi
  hermes gateway install --start-on-login --start-now >/dev/null
}
platform_macos_gateway_status() {
  local status
  status=$(hermes gateway status 2>/dev/null) || return 1
  printf '%s\n' "$status" | grep -Eiq '(^|[[:space:]:])(running|active)([[:space:]]|$)' || return 1
  ! printf '%s\n' "$status" | grep -Eiq 'not running|inactive|stopped'
}
platform_macos_gateway_service_present() { platform_macos_gateway_autostart registered; }
platform_macos_gateway_autostart() {
  local action=${1:-check}
  [[ $action == registered || $action == check || $action == ensure ]] || return 1
  # launchctl is diagnostic only; Hermes owns install/start.
  if ! launchctl list 2>/dev/null | grep -Eiq 'hermes.*gateway|gateway.*hermes'; then
    printf 'BLOCKED: Hermes gateway launchd registration was not found.\n' >&2
    return 3
  fi
  [[ $action != check ]] || platform_macos_gateway_status
}

platform_windows_require_tools() {
  local tool missing=0
  [[ -n ${BASH_VERSION:-} ]] || { platform_blocked 'real Git Bash'; return 3; }
  for tool in bash git curl; do
    if ! command -v "$tool" >/dev/null 2>&1 || ! "$tool" --version >/dev/null 2>&1; then
      printf 'BLOCKED: install Git for Windows with real Bash, git and curl; missing %s.\n' "$tool" >&2
      missing=1
    fi
  done
  if ! bash -c '[[ -n ${BASH_VERSION:-} && -n ${BASH_VERSINFO[0]:-} ]]' >/dev/null 2>&1; then
    printf 'BLOCKED: selected bash executable is ash/BusyBox; install real Git for Windows Bash.\n' >&2
    missing=1
  fi
  for tool in tar mktemp awk grep sed cygpath powershell.exe; do
    command -v "$tool" >/dev/null 2>&1 || { printf 'BLOCKED: required Windows Git Bash tool unavailable: %s\n' "$tool" >&2; missing=1; }
  done
  platform_python >/dev/null || missing=1
  ((missing == 0)) || return 3
}
platform_windows_check_os() { :; }
platform_windows_hermes_home() {
  local path=${HERMES_HOME:-}
  if [[ -z $path ]]; then
    [[ -n ${LOCALAPPDATA:-} ]] || { platform_blocked LOCALAPPDATA; return 3; }
    path="$LOCALAPPDATA/hermes"
  fi
  case $path in [A-Za-z]:*|\\\\*) platform_windows_convert_path to-posix "$path" ;; *) printf '%s\n' "$path" ;; esac
}
platform_windows_oaos_home() { printf '%s\n' "$HOME"; }
platform_windows_path() {
  case $1 in
    state) printf '%s/.oaos-install/state.json\n' "$HOME" ;;
    logs) printf '%s/.oaos/logs\n' "$HOME" ;;
    backups) printf '%s/.oaos/backups\n' "$HOME" ;;
    cache) printf '%s/.oaos/cache\n' "$HOME" ;;
    wiki) printf '%s/data/wiki\n' "$HOME" ;;
    scripts) printf '%s/scripts\n' "$(platform_hermes_home)" ;;
    *) return 1 ;;
  esac
}
platform_windows_package_prereqs() { :; }
platform_windows_package_install() { platform_blocked 'manual prerequisite installation'; }
platform_windows_install_hermes() {
  local native native_home
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info 'Would run saved official PowerShell Hermes installer.'; return 0; fi
  native=$(platform_windows_convert_path to-native "$1") || return 3
  native_home=$(platform_windows_convert_path to-native "$(platform_hermes_home)") || return 3
  HERMES_HOME=$native_home powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$native" -HermesHome "$native_home"
}
platform_windows_ps() { powershell.exe -NoProfile -NonInteractive -Command "$1"; }
platform_windows_memory_kib() {
  local value
  value=$(platform_windows_ps '[long]((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1KB)' | tr -d '\r') || return 3
  [[ $value =~ ^[0-9]+$ ]] || return 3
  printf '%s\n' "$value"
}
platform_windows_swap_state() {
  local value
  value=$(platform_windows_ps '[long]((Get-CimInstance Win32_PageFileUsage | Measure-Object -Property AllocatedBaseSize -Sum).Sum)' | tr -d '\r') || return 3
  [[ $value =~ ^[0-9]+$ ]] || return 3
  ((value > 0)) && printf '1\n' || printf '0\n'
}
platform_windows_disk_kib() { df -Pk "$1" | awk 'NR==2 {print $2, $4}'; }
platform_windows_timezone_get() { platform_windows_ps '(Get-TimeZone).Id' | tr -d '\r'; }
platform_windows_timezone_set() {
  printf 'BLOCKED: map IANA timezone %s to a verified Windows ID and set it manually in Windows Settings > Time & language > Date & time.\n' "$1" >&2
  return 3
}
platform_windows_swap_prepare() { info 'Windows manages the pagefile; inspect system virtual memory settings if capacity is insufficient.'; }
platform_windows_sleep_policy() { info 'Confirm Windows power and login policy manually for gateway availability.'; }
platform_windows_file_mode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }
platform_windows_secret_protect() {
  local native sid
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info "Would protect $1 with NTFS ACL."; return 0; fi
  native=$(platform_windows_convert_path to-native "$1") || return 3
  sid=$(platform_windows_ps '[Security.Principal.WindowsIdentity]::GetCurrent().User.Value' | tr -d '\r') || return 3
  [[ $sid =~ ^S-1- ]] || return 3
  icacls.exe "$native" /inheritance:r /grant:r "*$sid:(F)" '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' >/dev/null || return 3
  platform_windows_secret_check "$1"
}
platform_windows_secret_check() {
  local native
  [[ -f $1 ]] || return 1
  native=$(platform_windows_convert_path to-native "$1") || return 3
  # shellcheck disable=SC2016 # PowerShell variables are intentionally literal.
  OAOS_SECRET_NATIVE_PATH=$native platform_windows_ps '
$path = $env:OAOS_SECRET_NATIVE_PATH
$drive = New-Object System.IO.DriveInfo -ArgumentList ([System.IO.Path]::GetPathRoot($path))
if ($drive.DriveFormat -ne "NTFS") { exit 3 }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$acl = Get-Acl -LiteralPath $path
$owner = $acl.Owner
$ownerSid = $owner
try { $ownerSid = (New-Object System.Security.Principal.NTAccount -ArgumentList $owner).Translate([Security.Principal.SecurityIdentifier]).Value } catch {}
if ($ownerSid -ne $identity -or -not $acl.AreAccessRulesProtected) { exit 3 }
$allowed = @($identity, "S-1-5-18", "S-1-5-32-544")
foreach ($rule in $acl.Access) {
  $sid = $rule.IdentityReference.Value
  try { $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch {}
  if ($sid -notin $allowed) { exit 3 }
}
exit 0' >/dev/null 2>&1
}
platform_windows_sha256() {
  local value
  if command -v sha256sum >/dev/null 2>&1; then
    value=$(sha256sum "$1" 2>/dev/null | awk 'NR==1 {print $1}') || value=''
    if [[ $value =~ ^[[:xdigit:]]{64}$ ]]; then printf '%s\n' "$value" | tr '[:upper:]' '[:lower:]'; return; fi
  fi
  if command -v shasum >/dev/null 2>&1; then
    value=$(shasum -a 256 "$1" 2>/dev/null | awk 'NR==1 {print $1}') || value=''
    if [[ $value =~ ^[[:xdigit:]]{64}$ ]]; then printf '%s\n' "$value" | tr '[:upper:]' '[:lower:]'; return; fi
  fi
  if command -v certutil.exe >/dev/null 2>&1; then
    value=$(certutil.exe -hashfile "$(platform_windows_convert_path to-native "$1")" SHA256 | tr -d '\r' | grep -E '^[[:xdigit:] ]{64,}$' | awk 'NR==1 {gsub(/ /, ""); print}') || return 3
    [[ $value =~ ^[[:xdigit:]]{64}$ ]] || return 3
    printf '%s\n' "$value" | tr '[:upper:]' '[:lower:]'
    return
  fi
  platform_blocked 'SHA-256 tool'
}
platform_windows_atomic_write() { platform_portable_atomic_write "$1"; }
platform_windows_gateway_install() {
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info 'Would install Hermes gateway.'; return 0; fi
  HERMES_GATEWAY_INSTALL_START_ON_LOGIN=1 hermes gateway install --start-on-login --start-now >/dev/null
}
platform_windows_gateway_status() {
  local status
  status=$(hermes gateway status 2>/dev/null) || return 1
  printf '%s\n' "$status" | grep -Eiq '(^|[[:space:]:])(running|active)([[:space:]]|$)' || return 1
  ! printf '%s\n' "$status" | grep -Eiq 'not running|inactive|stopped'
}
platform_windows_gateway_service_present() { platform_windows_gateway_autostart registered; }
platform_windows_gateway_autostart() {
  local action=${1:-check} status task
  [[ $action == registered || $action == check || $action == ensure ]] || return 1
  status=$(hermes gateway status 2>/dev/null) || return 3
  task=$(printf '%s\n' "$status" | sed -n 's/.*\(Hermes_Gateway[^[:space:]]*\).*/\1/p' | awk 'NR==1 {print}')
  task=${task:-Hermes_Gateway}
  task=${task%:}
  if command -v schtasks.exe >/dev/null 2>&1 && schtasks.exe /Query /TN "$task" /XML 2>/dev/null | grep -Eiq '<([[:alnum:]_]+:)?LogonTrigger([[:space:]>])'; then
    :
  elif [[ -n ${APPDATA:-} ]]; then
    local startup
    startup=$(platform_windows_convert_path to-posix "$APPDATA/Microsoft/Windows/Start Menu/Programs/Startup") || return 3
    compgen -G "$startup/*Hermes*" >/dev/null || { platform_blocked 'gateway ONLOGON task or Startup fallback'; return 3; }
  else platform_blocked 'gateway ONLOGON task or Startup fallback'; return 3; fi
  [[ $action != check ]] || platform_windows_gateway_status
}

platform_portable_atomic_write() {
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info "Would atomically write $1."; return 0; fi
  local tmp
  tmp=$(mktemp "$1.tmp.XXXXXX") || return 1
  if cat > "$tmp" && platform_secret_protect "$tmp" && mv -f -- "$tmp" "$1" && platform_secret_check "$1"; then return 0; fi
  rm -f -- "$tmp"
  return 3
}
platform_portable_chat_probe() {
  local python
  python=$(platform_python) || return 3
  "$python" - <<'PY'
import subprocess
try:
    result = subprocess.run(['hermes', 'chat', '-q', 'Reply with exactly: OK'],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=30, check=False)
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit(1)
raise SystemExit(0 if b'OK' in result.stdout else 1)
PY
}
platform_portable_backup_prune() {
  local python
  [[ ${OAOS_DRY_RUN:-0} != 1 ]] || { info 'Would prune old verify backups.'; return 0; }
  python=$(platform_python) || return 3
  "$python" - "$(oaos_native_path "$1")" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
files = sorted((p for p in root.glob('hermes-verify-*.zip') if p.is_file()),
               key=lambda p: p.stat().st_mtime, reverse=True)
for path in files[2:]:
    path.unlink()
PY
}
platform_macos_chat_probe() { platform_portable_chat_probe; }
platform_windows_chat_probe() { platform_portable_chat_probe; }
platform_macos_backup_prune() { platform_portable_backup_prune "$1"; }
platform_windows_backup_prune() { platform_portable_backup_prune "$1"; }

# Public dispatch API; Linux implementation above stays unchanged.
platform_require_tools() { platform_dispatch require_tools; }
platform_check_os() { platform_dispatch check_os; }
platform_hermes_home() { platform_dispatch hermes_home; }
platform_oaos_home() { platform_dispatch oaos_home; }
platform_path() { platform_dispatch path "$@"; }
platform_package_prereqs() { platform_dispatch package_prereqs; }
platform_package_install() { platform_dispatch package_install "$@"; }
platform_install_hermes() { platform_dispatch install_hermes "$@"; }
platform_memory_kib() { platform_dispatch memory_kib; }
platform_swap_state() { platform_dispatch swap_state; }
platform_disk_kib() { platform_dispatch disk_kib "$@"; }
platform_timezone_get() { platform_dispatch timezone_get; }
platform_timezone_set() { platform_dispatch timezone_set "$@"; }
platform_swap_prepare() { platform_dispatch swap_prepare; }
platform_sleep_policy() { platform_dispatch sleep_policy; }
platform_file_mode() { platform_dispatch file_mode "$@"; }
platform_secret_protect() { platform_dispatch secret_protect "$@"; }
platform_secret_check() { platform_dispatch secret_check "$@"; }
platform_sha256() { platform_dispatch sha256 "$@"; }
platform_atomic_write() { platform_dispatch atomic_write "$@"; }
platform_gateway_install() { platform_dispatch gateway_install; }
platform_gateway_status() { platform_dispatch gateway_status; }
platform_gateway_service_present() { platform_dispatch gateway_service_present; }
platform_gateway_autostart() { platform_dispatch gateway_autostart "$@"; }
platform_chat_probe() { platform_dispatch chat_probe; }
platform_backup_prune() { platform_dispatch backup_prune "$@"; }
platform_dispatch() {
  local prefix
  case ${OAOS_PLATFORM:-} in
    linux) prefix=platform_linux_ ;;
    macos) prefix=platform_macos_ ;;
    windows-gitbash) prefix=platform_windows_ ;;
    *) platform_blocked "$1"; return 3 ;;
  esac
  "${prefix}${1}" "${@:2}"
}
