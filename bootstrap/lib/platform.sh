#!/usr/bin/env bash
# Personal platform boundary. Non-Linux implementations belong to P2/P3.

platform_blocked() {
  printf 'BLOCKED: %s is not implemented for %s (P2/P3).\n' "$1" "${OAOS_PLATFORM:-unknown}" >&2
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
    MINGW*|MSYS*|CYGWIN*)
      case ${MSYSTEM:-} in MINGW*|MSYS*|UCRT*) lane=windows-gitbash ;;
        *) printf 'BLOCKED: Windows requires Git Bash.\n' >&2; return 3 ;; esac ;;
    *) printf 'BLOCKED: unsupported OS: %s.\n' "$kernel" >&2; return 3 ;;
  esac
  if [[ $lane == macos && $arch != arm64 ]]; then
    printf 'BLOCKED: Personal macOS requires Apple Silicon.\n' >&2; return 3
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

platform_require_tools() {
  platform_linux platform_require_tools || return 3
  local tool
  for tool in bash git curl python3 tar mktemp awk grep sed; do
    command -v "$tool" >/dev/null 2>&1 || { printf 'BLOCKED: required tool unavailable: %s\n' "$tool" >&2; return 3; }
  done
}

platform_check_os() {
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

platform_hermes_home() { platform_linux platform_hermes_home || return 3; printf '%s/.hermes\n' "${HOME:?}"; }
platform_oaos_home() { platform_linux platform_oaos_home || return 3; printf '%s\n' "${HOME:?}"; }
platform_path() {
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
platform_package_prereqs() {
  platform_linux platform_package_prereqs || return 3
  local pkg
  for pkg in git curl xz-utils ca-certificates; do
    if ! command -v dpkg-query >/dev/null 2>&1 ||
       ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
      printf '%s\n' "$pkg"
    fi
  done
}

platform_package_install() {
  platform_linux platform_package_install || return 3
  (($#)) || return 0
  if command -v apt-get >/dev/null 2>&1; then
    run_root apt-get update && run_root apt-get install -y "$@"
  else
    warn 'Package manager unavailable; required packages need manual installation.'
    return 3
  fi
}

platform_install_hermes() {
  platform_linux platform_install_hermes || return 3
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info 'Would run saved official Hermes installer.'; return 0; fi
  bash "$1"
}

platform_memory_kib() { platform_linux platform_memory_kib || return 3; awk '/^MemTotal:/ {print $2}' /proc/meminfo; }
platform_swap_state() { platform_linux platform_swap_state || return 3; awk 'END {print NR-1}' /proc/swaps; }
platform_disk_kib() { platform_linux platform_disk_kib || return 3; df -Pk "$1" | awk 'NR==2 {print $2, $4}'; }
platform_timezone_get() { platform_linux platform_timezone_get || return 3; timedatectl show -p Timezone --value; }
platform_timezone_set() {
  platform_linux platform_timezone_set || return 3
  run_root timedatectl set-timezone "$1"
}

platform_swap_prepare() {
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

platform_sleep_policy() {
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

platform_file_mode() { platform_linux platform_file_mode || return 3; stat -c '%a' "$1"; }
platform_secret_protect() {
  platform_linux platform_secret_protect || return 3
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info "Would protect $1."; return 0; fi
  chmod 600 "$1"
}
platform_secret_check() {
  platform_linux platform_secret_check || return 3
  local parent mode
  [[ -f $1 && $(platform_file_mode "$1") == 600 ]] || return 1
  parent=$(dirname "$1")
  [[ -d $parent && -x $parent ]] || return 1
  mode=$(platform_file_mode "$parent") || return 1
  [[ $mode =~ ^[0-7]{3,4}$ ]] || return 1
  (( (8#$mode & 0022) == 0 ))
}
platform_sha256() { platform_linux platform_sha256 || return 3; sha256sum "$1" | awk '{print $1}'; }
platform_atomic_write() {
  platform_linux platform_atomic_write || return 3
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info "Would atomically write $1."; return 0; fi
  local tmp
  tmp=$(mktemp "$1.tmp.XXXXXX") || return 1
  if cat > "$tmp" && platform_secret_protect "$tmp" && mv -f -- "$tmp" "$1"; then return 0; fi
  rm -f -- "$tmp"
  return 1
}

platform_gateway_install() {
  platform_linux platform_gateway_install || return 3
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then info 'Would install Hermes gateway.'; return 0; fi
  hermes gateway install --start-on-login --start-now >/dev/null
}
platform_gateway_status() {
  platform_linux platform_gateway_status || return 3
  [[ -f $HOME/.config/systemd/user/hermes-gateway.service ]] || return 1
  hermes gateway status 2>/dev/null | grep -Eiq 'running|active'
}
platform_gateway_service_present() {
  platform_linux platform_gateway_service_present || return 3
  [[ -f $HOME/.config/systemd/user/hermes-gateway.service ]]
}
platform_gateway_autostart() {
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

platform_chat_probe() {
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

platform_backup_prune() {
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
