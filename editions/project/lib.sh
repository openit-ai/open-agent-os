#!/usr/bin/env bash
# Project helpers shared by editions/project/install.sh and bootstrap/verify/project-verify.sh.
# Load bootstrap/lib/common.sh, platform.sh and secret.sh first.
#
# Gate values live in the Hermes .env (owner-only, written by bootstrap/bin/oaos-secret-set).
# Secrets are never accepted from argv or the process environment; API tokens reach curl
# through a config on stdin (curl -K -), never through argv.

# Prints the value of KEY from the Hermes .env (unquoted the way oaos_secret_set writes it).
# Exit 0 with output when set and non-empty; 1 otherwise.
project_env_get() {
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

# Non-secret gate value: the Hermes .env first; otherwise the same-named process variable,
# which is then stored in the Hermes .env so later reruns do not need it.
project_gate_value() {
  local key=$1 value
  if value=$(project_env_get "$key"); then printf '%s' "$value"; return 0; fi
  value=${!key:-}
  [[ -n $value ]] || return 1
  if [[ ${OAOS_DRY_RUN:-0} != 1 ]]; then
    printf '%s' "$value" | oaos_secret_set "$key" 2>/dev/null || return 1
  fi
  printf '%s' "$value"
}

# Secrets are read from the Hermes .env only. A same-named process variable is refused,
# because it usually came from a command line (shell history, process list).
project_secret_value() {
  local key=$1
  if [[ -n ${!key:-} ]]; then
    warn "$key in the process environment is ignored; store it with: bash bootstrap/bin/oaos-secret-set $key"
  fi
  project_env_get "$key"
}

# Store a non-secret value (URL, allowlist) in the Hermes .env without argv.
project_env_put() {
  [[ ${OAOS_DRY_RUN:-0} != 1 ]] || return 0
  [[ $(project_env_get "$1" 2>/dev/null || true) == "$2" ]] && return 0
  printf '%s' "$2" | oaos_secret_set "$1" 2>/dev/null
}

# Tokens are embedded in a curl config, so only token-safe characters are accepted.
project_token_safe() { [[ $1 =~ ^[A-Za-z0-9_.-]{8,256}$ ]]; }

# project_api METHOD URL TOKEN [JSON]
# Prints the response body, then a final line with the HTTP status code.
# Exit 0 when curl completed (any HTTP status); 1 on a network error or unsafe token.
project_api() {
  local method=$1 url=$2 token=$3 data=${4:-}
  project_token_safe "$token" || return 1
  [[ $url =~ ^https?://[A-Za-z0-9.:/_-]+$ ]] || return 1
  {
    printf 'url = "%s"\n' "$url"
    printf 'request = "%s"\n' "$method"
    printf 'header = "Authorization: Bearer %s"\n' "$token"
    if [[ -n $data ]]; then
      printf 'header = "Content-Type: application/json"\n'
      printf 'data = "%s"\n' "${data//\"/\\\"}"
    fi
  } | curl -sS --max-time "${OAOS_API_TIMEOUT:-10}" -w '\n%{http_code}' -K - 2>/dev/null
}

# Mattermost: the token must belong to a bot account without the system_admin role.
# Prints a short reason; exit 0 only for a valid non-admin bot.
project_mattermost_bot_check() {
  local response code body
  response=$(project_api GET "${OAOS_MATTERMOST_API:-http://127.0.0.1:8065}/api/v4/users/me" "$1") ||
    { printf 'network error or invalid token characters'; return 1; }
  code=${response##*$'\n'}
  body=${response%$'\n'*}
  [[ $code == 200 ]] || { printf 'HTTP %s' "$code"; return 1; }
  printf '%s' "$body" | oaos_python -c '
import json, sys
try:
    user = json.load(sys.stdin)
except ValueError:
    print("unreadable response", end=""); sys.exit(1)
roles = str(user.get("roles", "")).split()
if user.get("is_bot") is not True:
    print("token belongs to a human account, not a bot", end=""); sys.exit(1)
if "system_admin" in roles:
    print("bot account has the system_admin role", end=""); sys.exit(1)
print("bot account without system_admin", end="")
'
}

# Mattermost user IDs are 26 lowercase alphanumerics, comma-separated.
project_valid_mattermost_ids() { [[ ${1//[[:space:]]/} =~ ^[a-z0-9]{26}(,[a-z0-9]{26})*$ ]]; }

# Every allowlisted ID must exist. Prints a short reason; exit 0 when all exist.
project_mattermost_ids_exist() {
  local ids=${2//[[:space:]]/} json response code body
  json="[\"${ids//,/\",\"}\"]"
  response=$(project_api POST "${OAOS_MATTERMOST_API:-http://127.0.0.1:8065}/api/v4/users/ids" "$1" "$json") ||
    { printf 'network error'; return 1; }
  code=${response##*$'\n'}
  body=${response%$'\n'*}
  [[ $code == 200 ]] || { printf 'HTTP %s' "$code"; return 1; }
  OAOS_EXPECTED_IDS=$ids oaos_python -c '
import json, os, sys
try:
    users = json.load(sys.stdin)
except ValueError:
    print("unreadable response", end=""); sys.exit(1)
expected = set(os.environ["OAOS_EXPECTED_IDS"].split(","))
found = {u.get("id") for u in users if isinstance(u, dict)}
missing = len(expected - found)
if missing:
    print("%d allowlisted ID(s) not found" % missing, end=""); sys.exit(1)
print("all allowlisted IDs exist", end="")
' <<< "$body"
}

# Outline: the token must belong to the AI-agent account whose email is the G9 bot
# mailbox, and that account must not be a workspace admin.
project_outline_agent_check() {
  local token=$1 mailbox=$2 response code body
  response=$(project_api POST "${OAOS_OUTLINE_API:-http://127.0.0.1:3000}/api/auth.info" "$token" '{}') ||
    { printf 'network error or invalid token characters'; return 1; }
  code=${response##*$'\n'}
  body=${response%$'\n'*}
  [[ $code == 200 ]] || { printf 'HTTP %s' "$code"; return 1; }
  OAOS_EXPECTED_MAILBOX=$mailbox oaos_python -c '
import json, os, sys
try:
    data = json.load(sys.stdin).get("data") or {}
except (ValueError, AttributeError):
    print("unreadable response", end=""); sys.exit(1)
user = data.get("user") or {}
email = str(user.get("email") or "").strip().lower()
if not email:
    print("auth.info did not return the account email", end=""); sys.exit(1)
if email != os.environ["OAOS_EXPECTED_MAILBOX"].strip().lower():
    print("token belongs to an account other than the G9 bot mailbox", end=""); sys.exit(1)
if user.get("role") == "admin" or user.get("isAdmin") is True:
    print("agent account is a workspace admin; make it a member or editor", end=""); sys.exit(1)
print("agent account (G9 mailbox, role %s)" % (user.get("role") or "member"), end="")
' <<< "$body"
}

# Gmail addresses get Google's IMAP/SMTP hosts by default.
project_mail_default_host() {
  local domain=${2##*@}
  domain=$(printf '%s' "$domain" | tr '[:upper:]' '[:lower:]')
  case $domain in
    gmail.com|googlemail.com) printf '%s.gmail.com' "$1" ;;
    *) return 1 ;;
  esac
}

# project_mail_login imap|smtp ADDRESS HOST: the password comes from the Hermes .env and
# reaches Python on stdin. Prints a short reason; exit 0 when the login succeeds.
project_mail_login() {
  local kind=$1 address=$2 host=$3 password
  password=$(project_env_get OAOS_MAIL_PASSWORD) || { printf 'OAOS_MAIL_PASSWORD absent'; return 1; }
  printf '%s' "$password" | OAOS_MAIL_KIND=$kind OAOS_MAIL_ADDRESS=$address OAOS_MAIL_HOST=$host \
    OAOS_MAIL_TIMEOUT=${OAOS_MAIL_TIMEOUT:-15} oaos_python -c '
import imaplib, os, smtplib, socket, ssl, sys
password = sys.stdin.read()
kind, address, host = os.environ["OAOS_MAIL_KIND"], os.environ["OAOS_MAIL_ADDRESS"], os.environ["OAOS_MAIL_HOST"]
timeout = float(os.environ["OAOS_MAIL_TIMEOUT"])
try:
    if kind == "imap":
        with imaplib.IMAP4_SSL(host, 993, timeout=timeout) as client:
            client.login(address, password)
            client.logout()
    else:
        with smtplib.SMTP(host, 587, timeout=timeout) as client:
            client.starttls(context=ssl.create_default_context())
            client.login(address, password)
except (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError):
    print("login rejected", end=""); sys.exit(1)
except (OSError, socket.timeout, smtplib.SMTPException, ssl.SSLError):
    print("connection failed", end=""); sys.exit(1)
print("login succeeded", end="")
'
}

# Hermes gateway runtime state: prints the recorded state of PLATFORM ("connected", ...).
project_gateway_platform_state() {
  local home
  home=$(platform_hermes_home) || return 1
  [[ -f $home/gateway_state.json ]] || return 1
  oaos_python - "$home/gateway_state.json" "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        state = json.load(f)
except (OSError, ValueError):
    sys.exit(1)
platform = (state.get("platforms") or {}).get(sys.argv[2]) or {}
print(platform.get("state") or "absent", end="")
PY
}

# 0 when `hermes gateway status` reports a running gateway (and not "not running").
project_gateway_running() {
  local status
  status=$(hermes gateway status 2>/dev/null || true)
  grep -Ei 'not running|inactive|stopped' >/dev/null <<< "$status" && return 1
  grep -Ei 'Gateway is running|gateway service is running' >/dev/null <<< "$status"
}

# Project ports and the package/unit that legitimately owns each.
# shellcheck disable=SC2034 # used by install.sh
project_ports='80:nginx 443:nginx 3000:oaos-outline 5432:postgresql 6379:redis-server 8065:mattermost'

# G0 sizing. Prints "cpus mem_kib disk_kib" (disk = size of /).
project_host_facts() {
  if [[ ${OAOS_TEST_MODE:-0} == 1 && -n ${OAOS_TEST_HOST_FACTS:-} ]]; then printf '%s\n' "$OAOS_TEST_HOST_FACTS"; return; fi
  printf '%s %s %s\n' "$(nproc)" "$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)" "$(df -Pk / | awk 'NR==2 {print $2}')"
}
# Recommended 4 vCPU / 16 GB / 200 GB; minimum 2 vCPU / 8 GB / 60 GB.
# Prints "<ok|warn|short>|<summary>|<below-recommendation details>|<below-minimum items>".
project_capacity() {
  local cpus mem disk below=() short=() level=ok
  read -r cpus mem disk < <(project_host_facts)
  [[ $cpus =~ ^[0-9]+$ && $mem =~ ^[0-9]+$ && $disk =~ ^[0-9]+$ ]] || return 1
  ((cpus >= 4)) || below+=("$cpus vCPU < 4")
  ((mem >= 15728640)) || below+=("$((mem / 1048576)) GiB RAM < 16 GB")
  ((disk >= 180000000)) || below+=("$((disk / 1000000)) GB disk < 200 GB")
  ((cpus >= 2)) || short+=(vCPU)
  ((mem >= 7340032)) || short+=(RAM)
  ((disk >= 60000000)) || short+=(disk)
  ((${#below[@]} == 0)) || level=warn
  ((${#short[@]} == 0)) || level=short
  printf '%s|%s vCPU / %s GiB / %s GB|%s|%s\n' "$level" "$cpus" "$((mem / 1048576))" "$((disk / 1000000))" \
    "$(IFS=';'; printf '%s' "${below[*]}")" "${short[*]}"
}
