#!/usr/bin/env bash
# Project helpers shared by editions/project/install.sh and bootstrap/verify/project-verify.sh.
# Load bootstrap/lib/common.sh, platform.sh, secret.sh and mail.sh first.
#
# Gate values live in the Hermes .env (owner-only, written by bootstrap/bin/oaos-secret-set).
# Secrets are never accepted from argv or the process environment; API tokens reach curl
# through a config on stdin (curl -K -), never through argv.

# Hermes .env accessors live in bootstrap/lib/secret.sh; these names are kept for the
# Project installer and verifier.
project_env_get() { oaos_env_get "$@"; }
project_gate_value() { oaos_gate_value "$@"; }
project_secret_value() { oaos_secret_value "$@"; }
project_env_put() { oaos_env_put "$@"; }

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

# IMAP/SMTP checks are shared with the Personal G5 stage (bootstrap/lib/mail.sh).
project_mail_default_host() { oaos_mail_default_host "$@"; }

# project_mail_login imap|smtp ADDRESS HOST: G9 login with OAOS_MAIL_PASSWORD from the Hermes .env.
project_mail_login() { oaos_mail_login "$1" "$2" "$3" OAOS_MAIL_PASSWORD; }

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
project_ports='80:nginx 443:nginx 3000:oaos-outline 5432:postgresql 6379:redis-server 8065:mattermost 8790:oaos-portal'
# Portal service (ADR 0002) on loopback; smoke tests may move it only with OAOS_TEST_MODE=1.
project_portal_port=8790
if [[ ${OAOS_TEST_MODE:-0} == 1 && ${OAOS_TEST_PORTAL_PORT:-} =~ ^[0-9]+$ ]]; then project_portal_port=$OAOS_TEST_PORTAL_PORT; fi
portal_dir() { printf '%s/oaos/portal' "$(oaos_home)"; }

# The single Mattermost team the G7 bot belongs to (the portal invites applicants to it).
# Prints the team ID, or a short reason with exit 1 when the bot is in no team or several.
project_mattermost_bot_team() {
  local response code body
  response=$(project_api GET "${OAOS_MATTERMOST_API:-http://127.0.0.1:8065}/api/v4/users/me/teams" "$1") ||
    { printf 'network error or invalid token characters'; return 1; }
  code=${response##*$'\n'}
  body=${response%$'\n'*}
  [[ $code == 200 ]] || { printf 'HTTP %s' "$code"; return 1; }
  oaos_python -c '
import json, re, sys
try:
    teams = [t for t in json.load(sys.stdin) if isinstance(t, dict) and not t.get("delete_at")]
except (ValueError, TypeError):
    print("unreadable response", end=""); sys.exit(1)
if len(teams) != 1:
    print("the bot belongs to %d teams" % len(teams), end=""); sys.exit(1)
team = str(teams[0].get("id", ""))
if not re.match(r"^[a-z0-9]{26}$", team):
    print("unreadable team ID", end=""); sys.exit(1)
print(team, end="")
' <<< "$body"
}

# 0 when the portal service on loopback answers its health endpoint; prints "apply=on|off".
project_portal_health() {
  local body
  body=$(curl -fsS --max-time 5 "http://127.0.0.1:$project_portal_port/api/portal/health" 2>/dev/null) || return 1
  oaos_python -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
if data.get("status") != "ok":
    sys.exit(1)
print("apply=%s" % data.get("apply", "?"), end="")
' <<< "$body"
}

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
