#!/usr/bin/env bash
# Secure secret input shared by every edition. Keep syntax compatible with macOS Bash 3.2.
#
# Interface (load common.sh and platform.sh first; os_detect runs on demand):
#
#   oaos_secret_set KEY
#       Reads ONE value for KEY from stdin. On a TTY it prompts on stderr and reads
#       with echo off (read -rs); otherwise it reads the whole of stdin and drops a
#       single trailing newline. It rejects an invalid KEY (UPPER_SNAKE only), an
#       empty value, and a value that still contains a newline or carriage return.
#       It then writes or replaces KEY=value in the Hermes .env
#       (${HERMES_HOME:-$HOME/.hermes}/.env; Windows default %LOCALAPPDATA%\hermes\.env)
#       through platform_atomic_write (temp file -> owner-only protection -> rename)
#       and reads the protection back with platform_secret_check (mode 600 and owner
#       on Linux/macOS, protected NTFS ACL on Windows). Values with spaces, '#' or
#       quotes are double-quoted the way Hermes itself writes .env lines.
#       The value never reaches argv, the log, the state file or stdout.
#       Exit: 0 stored; 1 invalid key/value or write error; 3 protection blocked.
#
#   oaos_hermes_env_file      prints the Hermes .env path.
#   oaos_secret_present KEY   exit 0 when KEY has a non-empty assignment in the .env; prints nothing.
#   oaos_env_get KEY          prints KEY's value from the .env (unquoted the way oaos_secret_set
#                             writes it); exit 0 when set and non-empty, 1 otherwise.
#   oaos_gate_value KEY       non-secret gate value: the .env first, otherwise the same-named
#                             process variable, which is then stored in the .env.
#   oaos_secret_value KEY     secret gate value: the .env only; a same-named process variable is
#                             refused with a warning (it usually came from a command line).
#   oaos_env_put KEY VALUE    stores a non-secret VALUE (URL, host, allowlist) without argv.
#
# Command-line wrapper: bash bootstrap/bin/oaos-secret-set KEY
# Typical use is the owner typing at the hidden prompt in their own terminal, so the
# value never enters chat or an agent transcript. Scripts pipe the value on stdin.

oaos_secret_platform_ready() {
  declare -F platform_atomic_write >/dev/null || { printf 'secret.sh needs platform.sh loaded first.\n' >&2; return 3; }
  [[ -n ${OAOS_PLATFORM:-} ]] || os_detect >/dev/null || return 3
}

oaos_hermes_env_file() {
  local home
  oaos_secret_platform_ready || return 3
  home=$(platform_hermes_home) || return 3
  [[ -n $home ]] || return 3
  printf '%s/.env\n' "$home"
}

oaos_secret_valid_key() { [[ $1 =~ ^[A-Z][A-Z0-9_]*$ ]]; }

# 0 when LINE assigns KEY (plain, `export KEY=` or `KEY = value`, as Hermes parses it).
oaos_env_line_defines() {
  [[ $1 =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*= ]] && [[ ${BASH_REMATCH[2]} == "$2" ]]
}

oaos_secret_present() {
  local env_file line value
  oaos_secret_valid_key "$1" || return 1
  env_file=$(oaos_hermes_env_file) || return 3
  [[ -f $env_file ]] || return 1
  while IFS= read -r line || [[ -n $line ]]; do
    if oaos_env_line_defines "$line" "$1"; then
      value=${line#*=}
      value=${value//[[:space:]\"\']/}
      [[ -n $value ]] && return 0
    fi
  done < "$env_file"
  return 1
}

oaos_secret_set() {
  local key=${1:-} value='' quoted env_file content='' line found=0
  if ! oaos_secret_valid_key "$key"; then
    printf 'Invalid key name; use UPPER_SNAKE_CASE (for example TELEGRAM_BOT_TOKEN).\n' >&2
    return 1
  fi
  oaos_secret_platform_ready || return 3
  if [[ -t 0 ]]; then
    printf 'Enter %s (input hidden, Enter to finish): ' "$key" >&2
    IFS= read -rs value || value=''
    printf '\n' >&2
  else
    value=$(cat; printf x) || { printf 'Could not read the value from stdin.\n' >&2; return 1; }
    value=${value%x}
    value=${value%$'\n'}
    value=${value%$'\r'}
  fi
  if [[ -z $value ]]; then
    printf 'Empty value rejected; nothing was written.\n' >&2
    return 1
  fi
  if [[ $value == *$'\n'* || $value == *$'\r'* ]]; then
    value=''
    printf 'Value contains a line break; rejected and nothing was written.\n' >&2
    return 1
  fi
  case $value in
    *[[:space:]\#\"\']*) quoted=${value//\\/\\\\}; quoted="\"${quoted//\"/\\\"}\"" ;;
    *) quoted=$value ;;
  esac
  value=''
  env_file=$(oaos_hermes_env_file) || return 3
  (umask 077; mkdir -p "$(dirname "$env_file")") || return 1
  if [[ -f $env_file ]]; then
    while IFS= read -r line || [[ -n $line ]]; do
      if oaos_env_line_defines "$line" "$key"; then
        ((found)) && continue
        content+="$key=$quoted"$'\n'
        found=1
      else
        content+="$line"$'\n'
      fi
    done < "$env_file" || return 1
  fi
  ((found)) || content+="$key=$quoted"$'\n'
  quoted=''
  if ! printf '%s' "$content" | platform_atomic_write "$env_file"; then
    content=''
    printf 'Could not write the Hermes .env safely; nothing was replaced.\n' >&2
    return 3
  fi
  content=''
  [[ ${OAOS_DRY_RUN:-0} == 1 ]] && return 0
  if ! platform_secret_check "$env_file"; then
    printf 'BLOCKED: the Hermes .env is not owner-only after writing (mode/owner or ACL check failed).\n' >&2
    return 3
  fi
  printf 'Stored %s in %s (value hidden; owner-only protection read back).\n' "$key" "$env_file" >&2
}

oaos_env_get() {
  local key=$1 env_file line value found=''
  oaos_secret_valid_key "$key" || return 1
  env_file=$(oaos_hermes_env_file) || return 1
  [[ -f $env_file ]] || return 1
  while IFS= read -r line || [[ -n $line ]]; do
    oaos_env_line_defines "$line" "$key" || continue
    value=${line#*=}
    value=${value#"${value%%[![:space:]]*}"}
    value=${value%"${value##*[![:space:]]}"}
    if [[ $value == \"*\" && ${#value} -ge 2 ]]; then
      value=${value:1:${#value}-2}
      value=${value//\\\"/\"}
      value=${value//\\\\/\\}
    elif [[ $value == \'*\' && ${#value} -ge 2 ]]; then
      value=${value:1:${#value}-2}
    fi
    found=$value
  done < "$env_file"
  [[ -n $found ]] || return 1
  printf '%s' "$found"
}

oaos_gate_value() {
  local key=$1 value
  if value=$(oaos_env_get "$key"); then printf '%s' "$value"; return 0; fi
  oaos_secret_valid_key "$key" || return 1
  value=${!key:-}
  [[ -n $value ]] || return 1
  if [[ ${OAOS_DRY_RUN:-0} != 1 ]]; then
    printf '%s' "$value" | oaos_secret_set "$key" 2>/dev/null || return 1
  fi
  printf '%s' "$value"
}

oaos_secret_value() {
  local key=$1
  oaos_secret_valid_key "$key" || return 1
  if [[ -n ${!key:-} ]]; then
    warn "$key in the process environment is ignored; store it with: bash bootstrap/bin/oaos-secret-set $key"
  fi
  oaos_env_get "$key"
}

oaos_env_put() {
  [[ ${OAOS_DRY_RUN:-0} != 1 ]] || return 0
  [[ $(oaos_env_get "$1" 2>/dev/null || true) == "$2" ]] && return 0
  printf '%s' "$2" | oaos_secret_set "$1" 2>/dev/null
}
