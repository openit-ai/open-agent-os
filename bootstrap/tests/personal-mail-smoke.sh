#!/usr/bin/env bash
# Personal G5 (optional mail stage): selection, store-then-verify logins, Himalaya config
# without the password, verify row PASS / MANUAL / FAIL / SKIP. Runs against a local
# plaintext IMAP/SMTP fixture (OAOS_TEST_MODE=1 only); no real mailbox is contacted.
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
server_pid=''
cleanup() {
  [[ -z $server_pid ]] || kill "$server_pid" 2>/dev/null || true
  rm -rf -- "$temp_root"
}
trap cleanup EXIT
for name in ${!OAOS_@}; do unset "$name"; done
unset HERMES_HOME MSYSTEM LOCALAPPDATA APPDATA XDG_CONFIG_HOME
export OAOS_ALLOW_FORK=1
install="$repo_root/editions/personal/install.sh"
verify="$repo_root/bootstrap/verify/personal-verify.sh"
password='abcdefghijklmnop'

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  for file in "$temp_root"/*.out "$temp_root"/*.err; do
    [[ -f $file ]] && { printf '\n--- %s ---\n' "$file" >&2; cat "$file" >&2; }
  done
  exit 1
}

# A PATH mirror without himalaya; host service managers are always mocked.
mirror="$temp_root/bin"
mkdir -p "$mirror"
for command_path in /usr/bin/* /bin/*; do
  [[ -f $command_path || -L $command_path ]] || continue
  name=${command_path##*/}
  [[ -e $mirror/$name || -L $mirror/$name || $name == himalaya || $name == hermes ]] || ln -s -- "$command_path" "$mirror/$name"
done
for name in systemctl loginctl ss; do rm -f -- "$mirror/$name"; printf '#!/usr/bin/env bash\nexit 0\n' > "$mirror/$name"; chmod 700 "$mirror/$name"; done
[[ ! -e $mirror/himalaya && -x $mirror/python3 ]] || fail 'PATH mirror invalid'

# Plaintext IMAP and SMTP fixture that accepts one password and logs each login result.
cat > "$temp_root/fake-mail.py" <<'PY'
import base64, os, socketserver, sys, threading
password, log = os.environ['FAKE_MAIL_PASSWORD'], os.environ['FAKE_MAIL_LOG']
def note(line):
    with open(log, 'a') as f:
        f.write(line + '\n')
class Imap(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b'* OK fake IMAP ready\r\n')
        for raw in self.rfile:
            parts = raw.decode().rstrip('\r\n').split(' ', 2)
            tag, cmd = parts[0], (parts[1].upper() if len(parts) > 1 else '')
            if cmd == 'CAPABILITY':
                self.wfile.write(b'* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n' + tag.encode() + b' OK done\r\n')
            elif cmd == 'LOGIN':
                user, secret = parts[2].split(' ', 1)
                if secret.startswith('"'):
                    secret = secret[1:-1].replace('\\"', '"').replace('\\\\', '\\')
                ok = secret == password
                note('imap %s %s' % (user.strip('"'), 'ok' if ok else 'rejected'))
                self.wfile.write(tag.encode() + (b' OK logged in\r\n' if ok else b' NO [AUTHENTICATIONFAILED] bad\r\n'))
            elif cmd == 'LOGOUT':
                self.wfile.write(b'* BYE\r\n' + tag.encode() + b' OK bye\r\n')
                return
            else:
                self.wfile.write(tag.encode() + b' BAD unknown\r\n')
class Smtp(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b'220 fake ESMTP\r\n')
        for raw in self.rfile:
            line = raw.decode().rstrip('\r\n')
            verb = line.split(' ', 1)[0].upper()
            if verb in ('EHLO', 'HELO'):
                self.wfile.write(b'250-fake\r\n250 AUTH PLAIN\r\n')
            elif verb == 'AUTH':
                blob = base64.b64decode(line.split(' ')[2]).split(b'\0')
                ok = blob[2].decode() == password
                note('smtp %s %s' % (blob[1].decode(), 'ok' if ok else 'rejected'))
                self.wfile.write(b'235 ok\r\n' if ok else b'535 bad\r\n')
            elif verb == 'QUIT':
                self.wfile.write(b'221 bye\r\n')
                return
            else:
                self.wfile.write(b'250 ok\r\n')
servers = [socketserver.ThreadingTCPServer(('127.0.0.1', 0), handler) for handler in (Imap, Smtp)]
for server in servers:
    threading.Thread(target=server.serve_forever, daemon=True).start()
with open(sys.argv[1] + '.tmp', 'w') as f:
    f.write('%d %d\n' % (servers[0].server_address[1], servers[1].server_address[1]))
os.rename(sys.argv[1] + '.tmp', sys.argv[1])
threading.Event().wait()
PY
FAKE_MAIL_PASSWORD=$password FAKE_MAIL_LOG="$temp_root/mail.log" python3 "$temp_root/fake-mail.py" "$temp_root/ports" &
server_pid=$!
for _ in $(seq 1 50); do [[ -s $temp_root/ports ]] && break; sleep 0.1; done
read -r imap_port smtp_port < "$temp_root/ports" || fail 'mail fixture did not start'

# Himalaya mock: runs the configured auth.cmd and accepts the expected password.
himalaya_bin="$temp_root/himalaya-bin"
mkdir -p "$himalaya_bin"
cat > "$himalaya_bin/himalaya" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MOCK_HIMALAYA_ARGV"
[[ $1 == --version ]] && { printf 'himalaya v1.1.0 +imap +smtp\n'; exit 0; }
[[ $1 == --config && $3 == envelope && $4 == list && $5 == --account && $6 == oaos-personal ]] || exit 2
command=$(sed -n 's/^backend\.auth\.cmd = "\(.*\)"$/\1/p' "$2")
[[ -n $command && $("$command") == "$MOCK_HIMALAYA_EXPECT" ]]
SH
chmod 700 "$himalaya_bin/himalaya"

home="$temp_root/home"
mkdir -p "$home/.hermes"
chmod 700 "$home/.hermes"
base_env=(HOME="$home" PATH="$mirror" OAOS_TEST_MODE=1 OAOS_TEST_MAIL_IMAP_PORT="$imap_port" OAOS_TEST_MAIL_SMTP_PORT="$smtp_port" OAOS_MAIL_TIMEOUT=5)
state_of() {
  python3 - "$home/.oaos-install/state.json" "$1" <<'PY'
import json, sys
stage = json.load(open(sys.argv[1]))['editions']['personal']['stages'][sys.argv[2]]
print(stage['status'] + '|' + stage['detail'])
PY
}
mail_row() {
  python3 - "$1" <<'PY'
import json, sys
row = [r for r in json.load(open(sys.argv[1]))['checks'] if r['check'] == 'Personal mail'][0]
print(row['status'] + '|' + row['evidence'])
PY
}
store() { printf '%s' "$2" | env -u OAOS_TEST_MODE HOME="$home" PATH="$mirror" bash "$repo_root/bootstrap/bin/oaos-secret-set" "$1" 2>/dev/null; }

# 1. Not selected: SKIP with a reason, no mail connection, installer exit 0.
env "${base_env[@]}" bash "$install" --stage mail > "$temp_root/skip.out" 2> "$temp_root/skip.err" || fail 'unselected mail stage did not exit 0'
[[ $(state_of mail) == SKIP\|not_selected:* ]] || fail "unselected mail stage state: $(state_of mail)"
[[ ! -e $temp_root/mail.log ]] || fail 'unselected mail stage contacted a mail server'
env "${base_env[@]}" bash "$verify" --json --offline > "$temp_root/v-skip.json" 2>/dev/null || true
[[ $(mail_row "$temp_root/v-skip.json") == SKIP\|*'not selected'* ]] || fail 'unselected mail row is not SKIP'
printf 'PASS: unselected G5 is SKIP(not_selected) in state and verify, with no mail connection\n'

# 2. Selected by the non-secret address (process value is stored); the password is required,
#    and a password in the process environment is refused.
code=0
env "${base_env[@]}" OAOS_PERSONAL_MAIL_ADDRESS=me@example.com OAOS_PERSONAL_MAIL_APP_PASSWORD="$password" \
  OAOS_PERSONAL_MAIL_IMAP_HOST=127.0.0.1 OAOS_PERSONAL_MAIL_SMTP_HOST=127.0.0.1 \
  bash "$install" --stage mail > "$temp_root/nopw.out" 2> "$temp_root/nopw.err" || code=$?
[[ $code == 3 && $(state_of mail) == blocked\|* ]] || fail "missing password was not blocked (exit $code)"
grep -Fq 'OAOS_PERSONAL_MAIL_APP_PASSWORD in the process environment is ignored' "$temp_root/nopw.err" || fail 'process-environment password not refused'
grep -Fq 'https://myaccount.google.com/apppasswords' "$temp_root/nopw.err" || fail 'G5 gate lacks the app-password link'
grep -Fq 'bash bootstrap/bin/oaos-secret-set OAOS_PERSONAL_MAIL_APP_PASSWORD' "$temp_root/nopw.err" || fail 'G5 gate lacks the exact variable'
grep -Fqx 'OAOS_PERSONAL_MAIL_ADDRESS=me@example.com' "$home/.hermes/.env" || fail 'address was not stored in the Hermes .env'
grep -Fq 'APP_PASSWORD' "$home/.hermes/.env" && fail 'process-environment password was stored'
printf 'PASS: G5 is selected by the stored address, refuses an environment password, names the link and variable\n'

# 3. Store-then-verify: a wrong password blocks on IMAP.
store OAOS_PERSONAL_MAIL_APP_PASSWORD wrongpassword || fail 'store wrong password'
code=0
env "${base_env[@]}" bash "$install" --stage mail > "$temp_root/wrong.out" 2> "$temp_root/wrong.err" || code=$?
[[ $code == 3 ]] && grep -Fq 'IMAP login to 127.0.0.1 failed (login rejected)' "$temp_root/wrong.err" || fail 'wrong password was not blocked at IMAP'
env "${base_env[@]}" bash "$verify" --json > "$temp_root/v-wrong.json" 2>/dev/null || true
[[ $(mail_row "$temp_root/v-wrong.json") == 'FAIL|IMAP login failed (login rejected)' ]] || fail "failing login row: $(mail_row "$temp_root/v-wrong.json")"
env "${base_env[@]}" bash "$verify" --json --offline > "$temp_root/v-offline.json" 2>/dev/null || true
[[ $(mail_row "$temp_root/v-offline.json") == SKIP\|Offline* ]] || fail 'offline mail row is not SKIP'
printf 'PASS: a rejected app password blocks the stage and FAILs the verify row (offline: SKIP)\n'

# 4. Correct password, Himalaya absent: stage done, MANUAL install link, verify MANUAL.
store OAOS_PERSONAL_MAIL_APP_PASSWORD "$password" || fail 'store password'
env "${base_env[@]}" bash "$install" --stage mail > "$temp_root/nohim.out" 2> "$temp_root/nohim.err" || fail 'mail stage with good logins failed'
[[ $(state_of mail) == done\|* ]] || fail 'mail stage not done after both logins'
grep -Fq 'G5 MANUAL: install Himalaya 1.x from https://pimalaya.org/himalaya/' "$temp_root/nohim.err" || fail 'Himalaya install link not reported'
grep -Fxq 'imap me@example.com ok' "$temp_root/mail.log" && grep -Fxq 'smtp me@example.com ok' "$temp_root/mail.log" || fail 'IMAP and SMTP logins not both performed'
code=0
env "${base_env[@]}" bash "$verify" --json > "$temp_root/v-manual.json" 2>/dev/null || code=$?
[[ $(mail_row "$temp_root/v-manual.json") == MANUAL\|*pimalaya.org* ]] || fail "Himalaya-absent row: $(mail_row "$temp_root/v-manual.json")"
printf 'PASS: good logins complete the stage; absent Himalaya is MANUAL with the install link\n'

# 5. Himalaya 1.x present: config written 600 without the password, password command 700.
him_env=("${base_env[@]}" PATH="$himalaya_bin:$mirror" MOCK_HIMALAYA_ARGV="$temp_root/himalaya.argv" MOCK_HIMALAYA_EXPECT="$password")
env "${him_env[@]}" bash "$install" --stage mail --recheck > "$temp_root/him.out" 2> "$temp_root/him.err" || fail 'mail stage with Himalaya failed'
config="$home/.config/himalaya/config.toml"
command="$home/.oaos/bin/oaos-mail-password"
[[ -f $config && $(stat -c %a "$config") == 600 ]] || fail 'Himalaya config absent or not 600'
[[ -x $command && $(stat -c %a "$command") == 700 ]] || fail 'password command absent or not 700'
grep -Fq "$password" "$config" "$command" && fail 'app password written into the Himalaya config or command'
grep -Fqx "backend.auth.cmd = \"$command\"" "$config" || fail 'IMAP auth.cmd does not use the password command'
grep -Fqx "message.send.backend.auth.cmd = \"$command\"" "$config" || fail 'SMTP auth.cmd does not use the password command'
grep -Fqx 'backend.host = "127.0.0.1"' "$config" || fail 'stored IMAP host not used'
[[ $(HOME="$home" "$command") == "$password" ]] || fail 'password command does not read the Hermes .env'
grep -Fq "$password" "$temp_root/himalaya.argv" && fail 'app password reached Himalaya argv'
grep -Fq 'Himalaya account oaos-personal reads the mailbox' "$temp_root/him.out" || fail 'Himalaya account check not reported'
env "${him_env[@]}" bash "$verify" --json > "$temp_root/v-pass.json" 2>/dev/null || true
[[ $(mail_row "$temp_root/v-pass.json") == PASS\|* ]] || fail "configured mail row: $(mail_row "$temp_root/v-pass.json")"
python3 - "$temp_root/v-pass.json" <<'PY' || fail 'password command tripped the secret-hygiene scan'
import json, sys
row = [r for r in json.load(open(sys.argv[1]))['checks'] if r['check'] == 'Secret hygiene'][0]
assert row['status'] == 'PASS', row
PY
printf 'PASS: Himalaya config (600) uses a 700 password command; no password in files or argv; verify PASS\n'

# 6. The owner's own Himalaya config is preserved (MANUAL until --confirm mail).
printf '[accounts.mine]\nemail = "me@example.com"\n' > "$config"
env "${him_env[@]}" bash "$install" --stage mail --recheck > "$temp_root/own.out" 2> "$temp_root/own.err" || fail 'mail stage with an owner config failed'
grep -Fq 'is your own Himalaya config and was preserved' "$temp_root/own.err" || fail 'owner Himalaya config not reported'
[[ $(head -n 1 "$config") == '[accounts.mine]' ]] || fail 'owner Himalaya config overwritten'
env "${him_env[@]}" bash "$verify" --json > "$temp_root/v-own.json" 2>/dev/null || true
[[ $(mail_row "$temp_root/v-own.json") == MANUAL\|* ]] || fail 'owner-config row is not MANUAL'
env "${him_env[@]}" bash "$verify" --json --confirm mail > "$temp_root/v-own-ok.json" 2>/dev/null || true
[[ $(mail_row "$temp_root/v-own-ok.json") == PASS\|*'owner confirmed'* ]] || fail '--confirm mail not recorded'
printf 'PASS: an owner Himalaya config is preserved; MANUAL resolves with --confirm mail\n'

# 7. No app password in logs, state or installer output.
grep -RFq "$password" "$home/.oaos" "$home/.oaos-install" "$temp_root"/*.out "$temp_root"/*.err "$temp_root"/*.json && fail 'app password leaked into logs, state or output'
(
  export OAOS_TEST_MODE=1 OAOS_TEST_PLATFORM=windows-gitbash
  # shellcheck source=bootstrap/lib/common.sh
  . "$repo_root/bootstrap/lib/common.sh"
  # shellcheck source=bootstrap/lib/platform.sh
  . "$repo_root/bootstrap/lib/platform.sh"
  os_detect >/dev/null
  code=0
  platform_path himalaya >/dev/null || code=$?
  [[ $code == 4 ]]
) || fail 'Windows lane does not leave Himalaya configuration manual'
printf 'PASS: no app password in logs, state or output; Windows Himalaya configuration stays manual\n'
