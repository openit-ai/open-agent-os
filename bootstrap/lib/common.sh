#!/usr/bin/env bash
# Shared functions for edition installers and verification runners.

oaos_home() { printf '%s' "${HOME:?HOME is required}"; }
oaos_state_file() { printf '%s/.oaos-install/state.json' "$(oaos_home)"; }
oaos_log_file() { printf '%s/.oaos/logs/%s-%s.log' "$(oaos_home)" "${OAOS_LOG_NAME:-install}" "$(date +%Y%m%d)"; }

redact() {
  # Keep these patterns in sync with personal-verify.sh secret scanning.
  sed -E \
    -e 's/(sk-|ghp_|nous_|AIza|xoxb-|AKIA|vck_)[A-Za-z0-9_-]+/[REDACTED]/g' \
    -e 's/[0-9]{8,10}:[A-Za-z0-9_-]{35,}/[REDACTED]/g' \
    -e 's/Bearer[[:space:]]+[A-Za-z0-9._~+\/=\-]+/Bearer [REDACTED]/Ig' \
    -e "s/((api[_-]?key|token|secret|password)[\"']?[[:space:]]*[:=][[:space:]]*[\"'])[^\"']*([\"'])/\\1[REDACTED]\\3/Ig" \
    -e "s/((api[_-]?key|token|secret|password)[\"']?[[:space:]]*[:=][[:space:]]*)[^[:space:]\"']+/\\1[REDACTED]/Ig"
}

log_line() {
  local level=$1 message=$2 line
  line="$(date '+%Y-%m-%dT%H:%M:%S%z') [$level] $message"
  if [[ ${OAOS_NO_LOG_FILE:-0} == 1 ]]; then
    printf '%s\n' "$line" | redact
  else
    mkdir -p "$(dirname "$(oaos_log_file)")"
    (umask 077; touch "$(oaos_log_file)")
    chmod 600 "$(oaos_log_file)" 2>/dev/null || true
    printf '%s\n' "$line" | redact | tee -a "$(oaos_log_file)"
  fi
}
info() { log_line INFO "$*"; }
warn() { log_line WARN "$*" >&2; }
die() { log_line ERROR "$*" >&2; exit 1; }
have_cmd() { command -v "$1" >/dev/null 2>&1; }

oaos_python() {
  have_cmd python3 || die 'python3 is required for JSON and HTTP checks.'
  python3 "$@"
}

stage_status() {
  local file
  file=$(oaos_state_file)
  if [[ ! -f $file ]]; then printf 'pending\n'; return; fi
  if have_cmd jq; then
    jq -r --arg name "$1" '.stages[$name].status // "pending"' "$file"
  else
    oaos_python - "$file" "$1" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    state = json.load(f)
print(state.get("stages", {}).get(sys.argv[2], {}).get("status", "pending"))
PY
  fi
}

stage_done() { [[ $(stage_status "$1") == 'done' ]]; }

state_valid() {
  local file
  file=$(oaos_state_file)
  [[ -e $file ]] || return 0
  if have_cmd jq; then jq -e . "$file" >/dev/null 2>&1; return; fi
  if have_cmd python3; then
    python3 - "$file" >/dev/null 2>&1 <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    json.load(f)
PY
    return
  fi
  return 0
}

stage_mark() {
  local name=$1 status=$2 detail=${3:-} file tmp now edition stages
  edition=${OAOS_STATE_EDITION:-personal}
  stages=${OAOS_STAGES:-prep hermes llm telegram gateway wiki harness cron verify}
  [[ $edition =~ ^[a-z]+$ && $stages =~ ^[a-z]+(\ [a-z]+)*$ ]] || die "Invalid edition stage configuration."
  [[ ${OAOS_DRY_RUN:-0} != 1 ]] || die 'Internal error: state write during dry run.'
  [[ $status == pending || $status == 'done' || $status == blocked || $status == failed ]] || die 'Invalid state status.'
  file=$(oaos_state_file)
  mkdir -p "$(dirname "$file")"
  tmp=$(mktemp "${file}.tmp.XXXXXX")
  chmod 600 "$tmp"
  now=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  if have_cmd jq; then
    if [[ -f $file ]]; then
      jq --arg edition "$edition" --arg stages "$stages" --arg name "$name" --arg status "$status" --arg detail "$detail" --arg now "$now" \
        '.edition=$edition | .updated_at=$now | .stages=(.stages // {}) | reduce ($stages | split(" "))[] as $s (. ; .stages[$s]=(.stages[$s] // {status:"pending",updated_at:$now,detail:""})) | .stages[$name]={status:$status,updated_at:$now,detail:$detail}' "$file" > "$tmp" || { rm -f -- "$tmp"; die 'Failed to write state file.'; }
    else
      jq -n --arg edition "$edition" --arg stages "$stages" --arg name "$name" --arg status "$status" --arg detail "$detail" --arg now "$now" \
        '{edition:$edition,updated_at:$now,stages:(reduce ($stages | split(" "))[] as $s ({}; .[$s]={status:"pending",updated_at:$now,detail:""}))} | .stages[$name]={status:$status,updated_at:$now,detail:$detail}' > "$tmp" || { rm -f -- "$tmp"; die 'Failed to write state file.'; }
    fi
  else
    oaos_python - "$file" "$tmp" "$name" "$status" "$detail" "$now" "$edition" "$stages" <<'PY' || { rm -f -- "$tmp"; die 'Failed to write state file.'; }
import json, os, sys
source, target, name, status, detail, now, edition, stage_names = sys.argv[1:]
state = {}
if os.path.isfile(source):
    with open(source, encoding="utf-8") as f:
        state = json.load(f)
state.update(edition=edition, updated_at=now)
stages = state.setdefault("stages", {})
for stage in stage_names.split():
    stages.setdefault(stage, dict(status="pending", updated_at=now, detail=""))
stages[name] = dict(status=status, updated_at=now, detail=detail)
with open(target, "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
  fi
  mv -f -- "$tmp" "$file"
}

run_root() {
  if [[ ${OAOS_DRY_RUN:-0} == 1 ]]; then
    info "Would run as root: $*"
    return 0
  fi
  if (( EUID == 0 )); then "$@"; return; fi
  if have_cmd sudo && sudo -n true >/dev/null 2>&1; then sudo -n "$@"; return; fi
  warn 'Root access unavailable; item blocked.'
  return 3
}

check_os() {
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
