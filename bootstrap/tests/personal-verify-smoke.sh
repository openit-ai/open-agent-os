#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
for name in ${!OAOS_@}; do unset "$name"; done
unset HERMES_HOME MSYSTEM LOCALAPPDATA APPDATA
export OAOS_TEST_MODE=1 OAOS_TEST_PLATFORM=linux OAOS_ALLOW_FORK=1
real_python=$(command -v python3)
real_git=$(command -v git)
export HOME="$temp_root/home"
mkdir -p "$HOME/.config/systemd/user/default.target.wants" "$HOME/.hermes/memories" "$HOME/data/wiki" "$HOME/.oaos" "$temp_root/bin"
printf '[Service]\n' > "$HOME/.config/systemd/user/hermes-gateway.service"
ln -s ../../hermes-gateway.service "$HOME/.config/systemd/user/default.target.wants/hermes-gateway.service"
for file in "$HOME/.hermes/SOUL.md" "$HOME/.hermes/memories/USER.md" "$HOME/.hermes/memories/MEMORY.md"; do
  printf 'test document\n' > "$file"
done
chmod 700 "$HOME/.hermes"
printf 'TELEGRAM_BOT_TOKEN=123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n' > "$HOME/.hermes/.env"
chmod 600 "$HOME/.hermes/.env"
printf 'model:\n  default: test\napi_key: ${OPENAI_API_KEY}\nmax_tokens: 4096\n' > "$HOME/.hermes/config.yaml"
printf '[Timer]\n' > "$HOME/.config/systemd/user/oaos-gateway-watchdog.timer"
git init -q -b main "$HOME/data/wiki"
printf 'wiki\n' > "$HOME/data/wiki/index.md"
git -C "$HOME/data/wiki" add index.md
git -C "$HOME/data/wiki" -c user.name=Test -c user.email=test@localhost commit -qm seed
git init -q --bare -b main "$HOME/data/wiki.git"
chmod 700 "$HOME/data/wiki.git"
git -C "$HOME/data/wiki" remote add origin "$HOME/data/wiki.git"
git -C "$HOME/data/wiki" push -q origin main
cat > "$temp_root/bin/hermes" <<'SH'
#!/usr/bin/env bash
case $1 in
  doctor)
    case ${MOCK_DOCTOR_CASE:-clean} in
      clean) exit 0 ;;
      advisory) printf '\033[33m⚠ browser npm vulnerability\033[0m\n1. run hermes setup\n1 issue(s) to address\n' >&2; exit 1 ;;
      failed) printf '\033[31m✗ broken check\033[0m\n1 issue(s) to address\n' >&2; exit 1 ;;
      no_summary) printf '⚠ advisory without summary\n' >&2; exit 1 ;;
    esac ;;
  gateway) [[ $2 == status ]] && { printf 'running\n'; exit 0; } ;;
  cron) [[ $2 == list ]] && { printf 'oaos-daily-backup\n'; exit 0; } ;;
  config) [[ $2 == get && $3 == --raw ]] && {
    case $4 in
      TELEGRAM_BOT_TOKEN) printf '123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n' ;;
      TELEGRAM_ALLOWED_USERS) printf '%s\n' "${MOCK_USERS:-12345,67890}" ;;
      approvals.cron_mode|approvals.single_query_mode|approvals.unattended_mode) printf '%s\n' "${MOCK_APPROVAL:-deny}" ;;
    esac
    exit 0; } ;;
  backup) [[ $2 == --output ]] && {
    printf '%s\n' "$3" >> "$MOCK_BACKUP_ARGS"
    target=$3
    if [[ $target == C:\\* ]]; then target=${target#C:\\}; target=/${target//\\//}; fi
    case ${MOCK_BACKUP_CASE:-success} in
      success) printf 'mock archive\n' > "$target"; exit 0 ;;
      skipped)
        printf 'mock archive\n' > "$target"
        printf 'Archive kept, but 1 FILE(S) COULD NOT BE ADDED:\nPRIVATE_BACKUP_PATH_MARKER: Permission denied\n' >&2
        exit 1 ;;
      missing)
        printf 'Archive kept, but 1 file(s) could not be added:\nPRIVATE_BACKUP_PATH_MARKER\n' >&2
        exit 1 ;;
      unreported)
        printf 'mock archive\n' > "$target"
        printf 'PRIVATE_BACKUP_PATH_MARKER: unexpected error\n' >&2
        exit 1 ;;
    esac
  } ;;
  chat) printf 'OK\n'; exit 0 ;;
esac
exit 1
SH
cat > "$temp_root/bin/loginctl" <<'SH'
#!/usr/bin/env bash
printf 'yes\n'
SH
# Host managers are mocked: the verifier must never query the real user session.
cat > "$temp_root/bin/systemctl" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat > "$temp_root/bin/ss" <<'SH'
#!/usr/bin/env bash
[[ -z ${MOCK_LISTENER:-} ]] || printf 'LISTEN 0 128 %s *:*\n' "$MOCK_LISTENER"
SH
chmod 700 "$temp_root/bin/hermes" "$temp_root/bin/loginctl" "$temp_root/bin/systemctl" "$temp_root/bin/ss"
export PATH="$temp_root/bin:$PATH"
export MOCK_BACKUP_ARGS="$temp_root/backup.args"

code=0
bash "$repo_root/bootstrap/verify/personal-verify.sh" --json --offline > "$temp_root/result.json" || code=$?
python3 - "$temp_root/result.json" "$code" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    result = json.load(f)
rows = result['checks']
assert len(rows) == 21, len(rows)
assert [row['number'] for row in rows] == list(range(1, 22))
names = [row['check'] for row in rows]
assert names[3] == 'Autostart registered' and names[4] == 'Reboot survival', names
for number in (1, 3, 4, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18):
    assert rows[number - 1]['status'] == 'PASS', rows[number - 1]
assert rows[1]['status'] == 'SKIP' and rows[7]['status'] == 'SKIP'
assert rows[4]['status'] == rows[5]['status'] == rows[6]['status'] == 'MANUAL'
assert rows[18]['status'] in ('PASS', 'FAIL')
assert int(sys.argv[2]) == (2 if rows[18]['status'] == 'PASS' else 1)
assert rows[19]['check'] == 'Personal mail' and rows[19]['status'] == 'SKIP', rows[19]
assert rows[20]['check'] == 'Wiki relay' and rows[20]['status'] == 'PASS', rows[20]
assert sum(result['summary'].values()) == 21
PY
grep -Fq 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' "$temp_root/result.json" && { printf 'FAIL: token in verifier output\n' >&2; exit 1; }
backups=("$HOME"/.oaos/backups/hermes-verify-*.zip)
[[ -s ${backups[0]} ]] || { printf 'FAIL: mock backup absent\n' >&2; exit 1; }
[[ $(head -n 1 "$MOCK_BACKUP_ARGS") == "$HOME"/* ]] || { printf 'FAIL: Linux backup path was not POSIX\n' >&2; exit 1; }
[[ $(stat -c %a "$HOME/.oaos/backups") == 700 ]] || { printf 'FAIL: backup dir not 700\n' >&2; exit 1; }
[[ $(stat -c %a "${backups[0]}") == 600 ]] || { printf 'FAIL: verify archive not 600\n' >&2; exit 1; }
printf 'PASS: Personal verifier has 21 JSON checks, offline SKIPs, MANUAL exit 2, owner-only backup\n'

# Exit 0 needs owner-confirmed MANUAL checks and no FAIL; each policy breach FAILs its row.
code=0
bash "$repo_root/bootstrap/verify/personal-verify.sh" --json --offline --confirm reboot,chat --confirm allowlist > "$temp_root/confirmed.json" || code=$?
python3 - "$temp_root/confirmed.json" "$code" <<'PY'
import json, sys
result = json.load(open(sys.argv[1]))
assert result['summary']['MANUAL'] == 0, result['summary']
assert int(sys.argv[2]) == (0 if result['summary']['FAIL'] == 0 else 1)
PY
chmod 644 "$HOME/.hermes/.env"
printf '{{NAME}}\n' >> "$HOME/.hermes/memories/USER.md"
printf 'telegram_token: "123456789:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"\n' >> "$HOME/.hermes/config.yaml"
rm -f "$HOME/.config/systemd/user/oaos-gateway-watchdog.timer"
git -C "$HOME/data/wiki" -c user.name=Test -c user.email=test@localhost commit -q --allow-empty -m 'not pushed'
code=0
MOCK_USERS='@someone' MOCK_APPROVAL=approve MOCK_LISTENER='0.0.0.0:9119' \
  bash "$repo_root/bootstrap/verify/personal-verify.sh" --json --offline > "$temp_root/breach.json" || code=$?
python3 - "$temp_root/breach.json" "$code" <<'PY'
import json, sys
rows = {row['check']: row for row in json.load(open(sys.argv[1]))['checks']}
for name in ('Allowlist configured', 'Scheduled jobs', 'Hermes .env protected', 'Config secret-free', 'Unattended approvals', 'Dashboard loopback', 'Wiki relay'):
    assert rows[name]['status'] == 'FAIL', rows[name]
assert '1 commit(s) behind' in rows['Wiki relay']['evidence'], rows['Wiki relay']
assert rows['Harness files']['status'] == 'MANUAL', rows['Harness files']
assert int(sys.argv[2]) == 1
PY
grep -Fq 'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB' "$temp_root/breach.json" && { printf 'FAIL: config secret printed\n' >&2; exit 1; }
chmod 600 "$HOME/.hermes/.env"
printf 'test document\n' > "$HOME/.hermes/memories/USER.md"
printf 'model:\n  default: test\n' > "$HOME/.hermes/config.yaml"
printf '[Timer]\n' > "$HOME/.config/systemd/user/oaos-gateway-watchdog.timer"
git -C "$HOME/data/wiki" push -q origin main
MOCK_LISTENER='127.0.0.1:9119' bash "$repo_root/bootstrap/verify/personal-verify.sh" --json --offline > "$temp_root/loopback.json" || true
python3 - "$temp_root/loopback.json" <<'PY'
import json, sys
rows = {row['check']: row for row in json.load(open(sys.argv[1]))['checks']}
assert rows['Dashboard loopback']['status'] == 'PASS', rows['Dashboard loopback']
assert rows['Wiki relay']['status'] == 'PASS', rows['Wiki relay']
PY
printf 'PASS: --confirm resolves MANUAL; .env mode, config secrets, allowlist, approvals, watchdog, non-loopback Dashboard, stale wiki relay FAIL\n'

mkdir -p "$temp_root/verify-tmp"
for backup_case in success skipped missing unreported; do
  code=0
  MOCK_BACKUP_CASE=$backup_case TMPDIR="$temp_root/verify-tmp" \
    bash "$repo_root/bootstrap/verify/personal-verify.sh" --json --offline > "$temp_root/backup-$backup_case.json" 2> "$temp_root/backup-$backup_case.err" || code=$?
  "$real_python" - "$temp_root/backup-$backup_case.json" "$backup_case" "$code" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    result = json.load(f)
row = result['checks'][17]
case, code = sys.argv[2], int(sys.argv[3])
assert code == (2 if result['summary']['FAIL'] == 0 else 1)
if case == 'success':
    assert row['status'] == 'PASS' and 'nonempty archive' in row['evidence'], row
elif case == 'skipped':
    assert row['status'] == 'PASS' and '1 unreadable file(s) skipped' in row['evidence'], row
elif case == 'missing':
    assert row['status'] == 'FAIL' and 'did not create an archive' in row['evidence'], row
else:
    assert row['status'] == 'FAIL' and 'without a counted skipped-file report' in row['evidence'], row
PY
  [[ ! -s $temp_root/backup-$backup_case.err ]] || { printf 'FAIL: backup output reached stderr\n' >&2; exit 1; }
  ! grep -Fq PRIVATE_BACKUP_PATH_MARKER "$temp_root/backup-$backup_case.json" || { printf 'FAIL: backup path reached status\n' >&2; exit 1; }
  [[ -z $(find "$temp_root/verify-tmp" -mindepth 1 -print -quit) ]] || { printf 'FAIL: verifier left temporary output\n' >&2; exit 1; }
done
printf 'PASS: backup exit/skip evidence branches conceal raw output and clean temporary files\n'

for doctor_case in advisory failed no_summary; do
  code=0
  MOCK_DOCTOR_CASE=$doctor_case bash "$repo_root/bootstrap/verify/personal-verify.sh" --json --offline > "$temp_root/$doctor_case.json" 2> "$temp_root/$doctor_case.err" || code=$?
  "$real_python" - "$temp_root/$doctor_case.json" "$doctor_case" "$code" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    row = json.load(f)['checks'][0]
case, code = sys.argv[2], int(sys.argv[3])
if case == 'advisory':
    assert row['status'] == 'PASS' and 'advisory findings only' in row['evidence'] and code in (1, 2), row
elif case == 'failed':
    assert row['status'] == 'FAIL' and 'failed check' in row['evidence'] and code == 1, row
else:
    assert row['status'] == 'FAIL' and 'no summary' in row['evidence'] and code == 1, row
PY
  [[ ! -s $temp_root/$doctor_case.err ]] || { printf 'FAIL: doctor output leaked\n' >&2; exit 1; }
done
printf 'PASS: doctor advisory, failed marker, and missing summary decisions conceal output\n'

mkdir -p "$temp_root/native-bin"
cat > "$temp_root/native-bin/uname" <<'SH'
#!/usr/bin/env bash
case $1 in -s) printf 'MINGW64_NT-10.0\n' ;; -m) printf 'x86_64\n' ;; *) /usr/bin/uname "$@" ;; esac
SH
cat > "$temp_root/native-bin/cygpath" <<'SH'
#!/usr/bin/env bash
case $1 in
  -w) path=${2#/}; printf 'C:\\%s\n' "${path//\//\\}" ;;
  -u) path=${2#C:\\}; printf '/%s\n' "${path//\\//}" ;;
  *) exit 1 ;;
esac
SH
cat > "$temp_root/native-bin/python3" <<'SH'
#!/usr/bin/env bash
args=()
for arg in "$@"; do
  case $arg in
    C:\\*) printf '%s\n' "$arg" >> "$MOCK_PYTHON_ARGV"; arg=${arg#C:\\}; arg=/${arg//\\//} ;;
    /tmp/*) printf 'POSIX path reached native Python: %s\n' "$arg" >&2; exit 90 ;;
  esac
  args+=("$arg")
done
exec "$REAL_PYTHON" "${args[@]}"
SH
cat > "$temp_root/native-bin/powershell.exe" <<'SH'
#!/usr/bin/env bash
case $* in
  *TotalPhysicalMemory*) printf '17179869184\r\n' ;;
  *Win32_PageFileUsage*) printf '4096\r\n' ;;
  *User.Value*) printf 'S-1-5-21-1000\r\n' ;;
  *Get-Acl*) exit 0 ;;
  *ParseFile*) exit 0 ;;
  *' -File '*) : > "$MOCK_HERMES_INSTALLED"; exit 0 ;;
  *) exit 1 ;;
esac
SH
cat > "$temp_root/native-bin/git" <<'SH'
#!/usr/bin/env bash
args=()
for arg in "$@"; do
  case $arg in
    C:\\*) printf '%s\n' "$arg" >> "$MOCK_GIT_ARGV"; arg=${arg#C:\\}; arg=/${arg//\\//} ;;
    /tmp/*) printf 'POSIX path reached native Git: %s\n' "$arg" >&2; exit 92 ;;
  esac
  args+=("$arg")
done
exec "$REAL_GIT" "${args[@]}"
SH
cat > "$temp_root/native-bin/icacls.exe" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat > "$temp_root/native-bin/schtasks.exe" <<'SH'
#!/usr/bin/env bash
printf '<Task><Triggers><LogonTrigger></LogonTrigger></Triggers></Task>\n'
SH
chmod 700 "$temp_root/native-bin/"*
(
  # shellcheck disable=SC2030 # This isolated Windows lane must not affect the Linux checks below.
  export PATH="$temp_root/native-bin:$PATH" HERMES_HOME="$HOME/.hermes"
  # shellcheck disable=SC2030
  export OAOS_TEST_MODE=1 OAOS_TEST_PLATFORM=windows-gitbash
  export REAL_PYTHON="$real_python" MOCK_PYTHON_ARGV="$temp_root/python.args"
  export REAL_GIT="$real_git" MOCK_GIT_ARGV="$temp_root/git.args"
  export MOCK_HERMES_INSTALLED="$temp_root/hermes-installed"
  code=0
  bash "$repo_root/bootstrap/verify/personal-verify.sh" --json --offline > "$temp_root/windows.json" 2> "$temp_root/windows.err" || code=$?
  "$real_python" - "$temp_root/windows.json" "$code" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    result = json.load(f)
rows = {row['check']: row for row in result['checks']}
for name in ('Hermes health', 'Wiki repository', 'Backup works', 'Hermes .env protected', 'Scheduled jobs'):
    assert rows[name]['status'] == 'PASS', rows[name]
assert 'no OAOS watchdog' in rows['Scheduled jobs']['evidence']
assert int(sys.argv[2]) == (2 if result['summary']['FAIL'] == 0 else 1)
PY
  [[ ! -s $temp_root/windows.err ]] || { printf 'FAIL: Windows verifier stderr\n' >&2; exit 1; }
  [[ $(tail -n 1 "$MOCK_BACKUP_ARGS") == C:\\* ]] || { printf 'FAIL: Windows backup output was not native\n' >&2; exit 1; }
  grep -Eq '^C:\\.*\\.oaos' "$MOCK_PYTHON_ARGV" || { printf 'FAIL: Windows secret scan or backup prune path was not native\n' >&2; exit 1; }
  grep -Eq '^C:\\.*\\data\\wiki$' "$MOCK_GIT_ARGV" || { printf 'FAIL: Windows wiki Git path was not native\n' >&2; exit 1; }

  # Exercise the no-jq state writer with a native Python that rejects POSIX argv.
  # shellcheck source=bootstrap/lib/common.sh
  . "$repo_root/bootstrap/lib/common.sh"
  # shellcheck source=bootstrap/lib/platform.sh
  . "$repo_root/bootstrap/lib/platform.sh"
  OAOS_PLATFORM=windows-gitbash
  export OAOS_PLATFORM
  have_cmd() { [[ $1 != jq ]] && command -v "$1" >/dev/null 2>&1; }
  platform_secret_protect() { :; }
  platform_secret_check() { :; }
  export OAOS_STATE_FILE="$HOME/.oaos-install/state.json"
  stage_mark prep applied 'native path state write' || { printf 'FAIL: Windows state writer\n' >&2; exit 1; }
  [[ -s $OAOS_STATE_FILE && $(stage_status prep) == applied ]] || { printf 'FAIL: Windows state file read-back\n' >&2; exit 1; }
  state_valid || { printf 'FAIL: Windows state validation\n' >&2; exit 1; }
  grep -Eq '^C:\\.*state.json' "$MOCK_PYTHON_ARGV" || { printf 'FAIL: native state source argv absent\n' >&2; exit 1; }
  grep -Eq '^C:\\.*state.json.tmp.' "$MOCK_PYTHON_ARGV" || { printf 'FAIL: native state target argv absent\n' >&2; exit 1; }

  cat > "$temp_root/native-bin/hermes" <<'SH'
#!/usr/bin/env bash
if [[ $1 == --version ]]; then [[ -f $MOCK_HERMES_INSTALLED ]]; exit; fi
[[ $1 == cron ]] || exit 1
case $2 in
  list) [[ ! -f $MOCK_CRON_JOBS ]] || cat "$MOCK_CRON_JOBS" ;;
  create)
    name='' script=''
    shift 2
    while (($#)); do
      case $1 in --name) name=$2; shift ;; --script) script=$2; shift ;; esac
      shift
    done
    [[ $script == "$name.sh" ]] || exit 91
    printf '%s\n' "$script" >> "$MOCK_CRON_ARGS"
    printf '%s\n' "$name" >> "$MOCK_CRON_JOBS" ;;
  *) exit 1 ;;
esac
SH
  chmod 700 "$temp_root/native-bin/hermes"
  export MOCK_CRON_JOBS="$temp_root/cron.jobs" MOCK_CRON_ARGS="$temp_root/cron.args"
  unset OAOS_STATE_FILE
  bash "$repo_root/editions/personal/install.sh" --stage cron > "$temp_root/cron.out" 2> "$temp_root/cron.err" || { cat "$temp_root/cron.err" >&2; printf 'FAIL: Windows cron stage\n' >&2; exit 1; }
  [[ $(cat "$MOCK_CRON_ARGS") == oaos-daily-backup.sh ]] || { printf 'FAIL: Windows cron must register only the backup script by filename\n' >&2; exit 1; }
  bash "$HOME/.hermes/scripts/oaos-daily-backup.sh" || { printf 'FAIL: generated Windows backup script\n' >&2; exit 1; }
  [[ -n $(find "$HOME/.oaos/backups" -maxdepth 1 -name 'oaos-*.tar.gz' -print -quit) ]] || { printf 'FAIL: generated Windows backup archive absent\n' >&2; exit 1; }

  backup_script="$HOME/.hermes/scripts/oaos-daily-backup.sh"
  watchdog_script="$HOME/.oaos/bin/oaos-gateway-watchdog.sh"
  cp "$backup_script" "$temp_root/original-backup.sh"
  cp "$watchdog_script" "$temp_root/original-watchdog.sh"
  : > "$MOCK_CRON_JOBS"
  export OAOS_STATE_FILE="$temp_root/cron-identical-state.json"
  bash "$repo_root/editions/personal/install.sh" --stage cron > "$temp_root/cron-identical.out" 2> "$temp_root/cron-identical.err" || { cat "$temp_root/cron-identical.err" >&2; printf 'FAIL: identical Windows cron scripts blocked rerun\n' >&2; exit 1; }
  [[ $(wc -l < "$MOCK_CRON_ARGS") == 2 ]] || { printf 'FAIL: identical Windows cron scripts were not registered\n' >&2; exit 1; }
  if ! { cmp -s "$backup_script" "$temp_root/original-backup.sh" && cmp -s "$watchdog_script" "$temp_root/original-watchdog.sh"; }; then
    printf 'FAIL: identical Windows cron scripts changed\n' >&2; exit 1
  fi

  printf 'user-edited backup\n' > "$backup_script"
  : > "$MOCK_CRON_JOBS"
  export OAOS_STATE_FILE="$temp_root/cron-different-backup-state.json"
  code=0
  bash "$repo_root/editions/personal/install.sh" --stage cron > "$temp_root/cron-different-backup.out" 2> "$temp_root/cron-different-backup.err" || code=$?
  [[ $code == 3 && $(cat "$backup_script") == 'user-edited backup' ]] || { printf 'FAIL: changed Windows backup script was not preserved\n' >&2; exit 1; }
  [[ -z $(find "$HOME/.hermes/scripts" -name '*.tmp.*' -print -quit) ]] || { printf 'FAIL: cron left a temporary script\n' >&2; exit 1; }

  cp "$temp_root/original-backup.sh" "$backup_script"
  printf 'user-edited watchdog\n' > "$watchdog_script"
  : > "$MOCK_CRON_JOBS"
  export OAOS_STATE_FILE="$temp_root/cron-different-watchdog-state.json"
  code=0
  bash "$repo_root/editions/personal/install.sh" --stage cron > "$temp_root/cron-different-watchdog.out" 2> "$temp_root/cron-different-watchdog.err" || code=$?
  [[ $code == 3 && $(cat "$watchdog_script") == 'user-edited watchdog' ]] || { printf 'FAIL: changed Windows watchdog script was not preserved\n' >&2; exit 1; }
  [[ -z $(find "$HOME/.hermes/scripts" "$HOME/.oaos/bin" -name '*.tmp.*' -print -quit) ]] || { printf 'FAIL: cron left a temporary script\n' >&2; exit 1; }
  unset OAOS_STATE_FILE

  rm -rf -- "$HOME/data/wiki"
  bash "$repo_root/editions/personal/install.sh" --stage wiki > "$temp_root/wiki.out" 2> "$temp_root/wiki.err" || { cat "$temp_root/wiki.err" >&2; printf 'FAIL: Windows wiki stage\n' >&2; exit 1; }
  [[ -d $HOME/data/wiki/.git ]] || { printf 'FAIL: Windows wiki init absent\n' >&2; exit 1; }
  grep -Eq '^C:\\.*\\data\\wiki$' "$MOCK_GIT_ARGV" || { printf 'FAIL: Windows wiki init path was not native\n' >&2; exit 1; }

  cat > "$temp_root/native-bin/curl" <<'SH'
#!/usr/bin/env bash
output=''
while (($#)); do
  if [[ $1 == -o ]]; then output=$2; break; fi
  shift
done
[[ $output == C:\\* ]] || exit 93
printf '%s\n' "$output" > "$MOCK_CURL_ARGV"
output=${output#C:\\}; output=/${output//\\//}
printf '# mock PowerShell installer\n' > "$output"
SH
  chmod 700 "$temp_root/native-bin/curl"
  export MOCK_CURL_ARGV="$temp_root/curl.args"
  code=0
  bash "$repo_root/editions/personal/install.sh" --yes --stage hermes > "$temp_root/hermes-nosha.out" 2> "$temp_root/hermes-nosha.err" || code=$?
  [[ $code == 3 && ! -f $MOCK_HERMES_INSTALLED && ! -e $MOCK_CURL_ARGV ]] || { printf 'FAIL: Windows install.ps1 ran without a pinned checksum\n' >&2; exit 1; }
  OAOS_HERMES_INSTALLER_SHA256=$("$real_python" -c 'import hashlib; print(hashlib.sha256(b"# mock PowerShell installer\n").hexdigest())')
  export OAOS_HERMES_INSTALLER_SHA256
  bash "$repo_root/editions/personal/install.sh" --yes --stage hermes > "$temp_root/hermes.out" 2> "$temp_root/hermes.err" || { cat "$temp_root/hermes.err" >&2; printf 'FAIL: Windows Hermes installer stage\n' >&2; exit 1; }
  [[ -f $MOCK_HERMES_INSTALLED ]] || { printf 'FAIL: Windows Hermes installer not invoked\n' >&2; exit 1; }
  grep -Eq '^C:\\.*\\hermes-install.ps1$' "$MOCK_CURL_ARGV" || { printf 'FAIL: Windows curl output path was not native\n' >&2; exit 1; }
)
printf 'PASS: Windows verifier, state writer, cron filenames and idempotence, wiki Git, curl, and backup script\n'

# The model probe uses a bounded subprocess and accepts only the expected reply.
# shellcheck source=bootstrap/lib/platform.sh
. "$repo_root/bootstrap/lib/platform.sh"
OAOS_PLATFORM=linux
platform_chat_probe || { printf 'FAIL: model probe mock reply\n' >&2; exit 1; }
printf 'PASS: bounded model probe accepts mock reply\n'
