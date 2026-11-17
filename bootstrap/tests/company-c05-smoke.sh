#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Run with: pg_virtualenv bash -e -o pipefail bootstrap/tests/company-c05-smoke.sh
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
[[ -n ${PGPORT:-} ]] || { printf 'Temporary PostgreSQL (pg_virtualenv) is required.\n' >&2; exit 3; }
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
mkdir -p "$temp_root/home/.oaos-install" "$temp_root/home/.config/oaos-company" \
  "$temp_root/bin" "$temp_root/backup" "$temp_root/key-backup" "$temp_root/rollback"
chmod 700 "$temp_root/backup" "$temp_root/key-backup" "$temp_root/rollback"
chmod 700 "$temp_root/home/.config/oaos-company"
printf 'server { listen 443 ssl; }\n' > "$temp_root/vhost.conf"
python3 - "$temp_root/home/.oaos-install/company-state.json" <<'PY'
import json, sys
with open(sys.argv[1], 'w', encoding='utf-8') as out:
    json.dump({'edition': 'company', 'stages': {'c01': {'status': 'verified'}, 'c02': {'status': 'verified'}}}, out)
PY
cat > "$temp_root/bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
case ${1:-} in
  --user) case ${2:-} in
    show-environment) [[ ${OAOS_C05_NO_USER_MANAGER:-0} != 1 ]]; exit $? ;;
    list-unit-files) exit 0 ;;
  esac ;;
  is-enabled) printf 'enabled\n'; exit 0 ;;
  is-active) printf 'active\n'; exit 0 ;;
esac
exit 1
MOCK
cat > "$temp_root/bin/loginctl" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
cat > "$temp_root/bin/nginx" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
cat > "$temp_root/bin/openssl" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
cat > "$temp_root/bin/ss" <<'MOCK'
#!/usr/bin/env bash
printf 'State Recv-Q Send-Q Local Address:Port Peer Address:Port\nLISTEN 0 5 127.0.0.1:8765 0.0.0.0:*\n'
MOCK
cat > "$temp_root/bin/sudo" <<'MOCK'
#!/usr/bin/env bash
[[ ${1:-} == -n ]] || exit 1
shift
"$@"
MOCK
cat > "$temp_root/bin/getent" <<'MOCK'
#!/usr/bin/env bash
if [[ ${1:-} == passwd ]]; then
  printf '%s:x:1000:1000::%s:/bin/bash\n' "$2" "$HOME"
else
  /usr/bin/getent "$@"
fi
MOCK
cat > "$temp_root/bin/runuser" <<'MOCK'
#!/usr/bin/env bash
[[ ${1:-} == -u && ${2:-} == postgres && ${3:-} == -- ]] || exit 1
shift 3
"$@"
MOCK
chmod 700 "$temp_root/bin/"*

export HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" OAOS_NO_LOG_FILE=1
export OAOS_COMPANY_USER OAOS_COMPANY_E_ID=E-c05-test
OAOS_COMPANY_USER=$(id -un)
export PGHOST=localhost PGHOSTADDR=127.0.0.1
export OAOS_COMPANY_PG_SOCKET_DIR=/tmp
export OAOS_COMPANY_NGINX_VHOST="$temp_root/vhost.conf"
export OAOS_COMPANY_BACKUP_DIR="$temp_root/backup"
export OAOS_COMPANY_KEY_BACKUP_DIR="$temp_root/key-backup"
export OAOS_COMPANY_ROLLBACK_DIR="$temp_root/rollback"
export OAOS_COMPANY_HTTPS_ORIGIN=https://e.example.test
printf 'OAOS_COMPANY_USER=%s\nOAOS_COMPANY_E_ID=%s\nPGDATABASE=%s\nPGHOST=%s\nPGUSER=%s\nOAOS_COMPANY_HTTPS_ORIGIN=%s\nOAOS_COMPANY_NGINX_VHOST=%s\nOAOS_COMPANY_BACKUP_DIR=%s\nOAOS_COMPANY_KEY_BACKUP_DIR=%s\nOAOS_COMPANY_ROLLBACK_DIR=%s\n' \
  "$OAOS_COMPANY_USER" "$OAOS_COMPANY_E_ID" "$PGDATABASE" "$PGHOST" "$PGUSER" \
  "$OAOS_COMPANY_HTTPS_ORIGIN" "$OAOS_COMPANY_NGINX_VHOST" "$OAOS_COMPANY_BACKUP_DIR" \
  "$OAOS_COMPANY_KEY_BACKUP_DIR" "$OAOS_COMPANY_ROLLBACK_DIR" \
  > "$HOME/.config/oaos-company/deployment.conf"
chmod 600 "$HOME/.config/oaos-company/deployment.conf"
printf 'PGDATABASE=%s\n' "$PGDATABASE" > "$HOME/.config/oaos-company/.env"
chmod 600 "$HOME/.config/oaos-company/.env"

code=0
bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/blocked" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'C04 verified gate'
grep -Fq 'C01, C02, C03 and C04 must be verified' "$temp_root/blocked" || fail 'gate reason'
python3 - "$HOME/.oaos-install/company-state.json" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding='utf-8') as src:
    state = json.load(src)
state['stages']['c03'] = {'status': 'verified'}
state['stages']['c04'] = {'status': 'verified'}
with open(path, 'w', encoding='utf-8') as out:
    json.dump(state, out)
PY
code=0
OAOS_C05_NO_USER_MANAGER=1 bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/no-manager" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'systemd user-manager gate'
grep -Fq 'systemd user manager is unavailable' "$temp_root/no-manager" || fail 'systemd gate reason'
code=0
PGPORT=1 bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/no-db" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'database connection gate'
grep -Fq 'E database connection failed' "$temp_root/no-db" || fail 'database gate reason'

# The temporary cluster authenticates its creator; role startup options make
# all Company SQL run as a separate non-superuser without a password argument.
psql -X -q -v ON_ERROR_STOP=1 -c 'CREATE ROLE c05_test NOSUPERUSER NOBYPASSRLS' >/dev/null
psql -X -q -v ON_ERROR_STOP=1 -c "GRANT CREATE ON DATABASE \"$PGDATABASE\" TO c05_test" >/dev/null
code=0
env -u PGOPTIONS bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/bypass-role" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'RLS bypass role gate'
grep -Fq 'database role bypasses RLS' "$temp_root/bypass-role" || fail 'RLS bypass gate reason'
export PGOPTIONS='-c role=c05_test'
bash "$repo_root/editions/company/lib/migrate.sh" --through 3 >/dev/null

bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/installed" || fail 'C05 install'
[[ $(psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT max(version) FROM company.schema_migrations WHERE version < 13') == 4 ]] || fail 'migration 4 missing'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'pre-migration backup'
[[ $(find "$OAOS_COMPANY_KEY_BACKUP_DIR" -name company.env | wc -l) == 1 ]] || fail 'restricted key backup'
bash -e -o pipefail -c 'python3 "$1" check-key "$2"' _ "$repo_root/editions/company/app/vault.py" "$HOME/.config/oaos-company/.env" || fail 'AES-GCM key presence'
mv "$HOME/.config/oaos-company/.env" "$HOME/.config/oaos-company/.env.saved"
printf 'PGDATABASE=%s\n' "$PGDATABASE" > "$HOME/.config/oaos-company/.env"
chmod 600 "$HOME/.config/oaos-company/.env"
code=0
bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/missing-key" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'applied migration must not regenerate a missing key'
mv "$HOME/.config/oaos-company/.env.saved" "$HOME/.config/oaos-company/.env"
bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/restored-key" || fail 'restored key rerun'
[[ $(find "$OAOS_COMPANY_KEY_BACKUP_DIR" -name company.env | wc -l) == 1 ]] || fail 'missing key gate created backup'
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c01 --read-back > "$temp_root/c01-regression" || fail 'C01 schema read-back after C05'
grep -Fq 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/c01-regression" || fail 'C01 regression summary'
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c04 --read-back > "$temp_root/c04-regression" || fail 'C04 assistant read-back after C05'
grep -Fq 'Summary: PASS=5 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/c04-regression" || fail 'C04 regression summary'
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c05 --read-back > "$temp_root/verified" || fail 'C05 real DB read-back'
grep -Fq 'Summary: PASS=8 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/verified" || fail 'read-back summary'
grep '^Summary:' "$temp_root/verified"
[[ $(psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.organizations WHERE slug LIKE 'c05-%'") == 0 ]] || fail 'fixture rollback'
[[ $(python3 - "$HOME/.oaos-install/company-state.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding='utf-8'))['stages']['c05']['status'])
PY
) == verified ]] || fail 'verified checkpoint'
bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/rerun" || fail 'C05 idempotent rerun'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'rerun created unnecessary backup'
[[ $(find "$OAOS_COMPANY_KEY_BACKUP_DIR" -name company.env | wc -l) == 1 ]] || fail 'rerun created unnecessary key backup'
printf 'PASS: C05 gates, migration 4, backups, SQL responses, rollback, idempotence\n'
printf 'PASS: C01 and C04 read-back remain compatible after migration 4\n'

export OAOS_C05_REPO_ROOT="$repo_root"
python3 - <<'PY'
import os
import sys
sys.path.insert(0, os.path.join(os.environ['OAOS_C05_REPO_ROOT'], 'editions', 'company', 'app'))
from vault import VaultAccessDenied, open_envelope, seal
key = os.urandom(32)
value = os.urandom(40)
first, first_nonce = seal(value, key, 'org', 'owner-a', 'provider', 'purpose')
assert open_envelope(first, first_nonce, key, 'org', 'owner-a', 'provider', 'purpose') == value
second, second_nonce = seal(os.urandom(40), key, 'org', 'owner-a', 'provider', 'purpose')
assert first != second and first_nonce != second_nonce
for envelope, nonce, owner in ((first, first_nonce, 'owner-b'), (first, second_nonce, 'owner-a')):
    try:
        open_envelope(envelope, nonce, key, 'org', owner, 'provider', 'purpose')
    except VaultAccessDenied:
        pass
    else:
        raise AssertionError('vault binding was not authenticated')
PY
printf 'PASS: AES-256-GCM round trip, owner binding and nonce authentication\n'

# O5 vault key rotation: two owners in two organizations store real envelopes
# (one written before key versions were bound in the AAD, one after), then the
# rotation re-encrypts both to V2 without changing their generation and
# retires V1 only after the database reads back zero rows on it.
env_file="$HOME/.config/oaos-company/.env"
vault="$repo_root/editions/company/app/vault.py"
app_dir="$repo_root/editions/company/app"
cat > "$temp_root/vault_fixture.py" <<'FIXTURE'
import base64, os, sys, uuid
sys.path.insert(0, sys.argv[1])
import vault
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
mode, env_path, work = sys.argv[2], sys.argv[3], sys.argv[4]
if mode == 'seed':
    current, keys = vault.load_store(env_path)
    for n, legacy in ((1, True), (2, False)):
        org, owner = str(uuid.uuid4()), str(uuid.uuid4())
        secret = ('rotation-secret-%d-' % n).encode() + os.urandom(8).hex().encode()
        if legacy:  # envelope as written before O5 (no key version in the AAD)
            nonce = os.urandom(12)
            ciphertext = AESGCM(keys[1]).encrypt(
                nonce, secret, vault._aad(org, owner, 'slack', 'delegation'))
        else:
            ciphertext, nonce = vault.seal(secret, keys[current], org, owner, 'slack',
                                           'delegation', current)
        with open(os.path.join(work, 'plain-%d' % n), 'wb') as f:
            f.write(secret)
        with open(os.path.join(work, 'ids-%d' % n), 'w') as f:
            f.write('%s %s\n' % (org, owner))
        sys.stdout.write(
            "BEGIN;\n"
            "SELECT set_config('company.org_id', '%s', true), set_config('company.member_id', '%s', true);\n"
            "INSERT INTO company.organizations(id, slug) VALUES ('%s', 'rot-%s');\n"
            "INSERT INTO company.members(id, org_id, project_user_id, role, state)"
            " VALUES ('%s', '%s', 'rot-%s', 'admin', 'active');\n"
            "SELECT company.register_personal_assistant();\n"
            "SELECT company.store_personal_secret('slack', 'delegation', decode('%s', 'base64'),"
            " decode('%s', 'base64'), %d);\nCOMMIT;\n"
            % (org, owner, org, org, owner, org, owner,
               base64.b64encode(ciphertext).decode(), base64.b64encode(nonce).decode(), current))
elif mode == 'check':
    expected = int(sys.argv[5])
    current, keys = vault.load_store(env_path)
    assert current == expected and sorted(keys) == [current], 'retired key still present'
    plain = {}
    for n in (1, 2):
        org = open(os.path.join(work, 'ids-%d' % n)).read().split()[0]
        plain[org] = open(os.path.join(work, 'plain-%d' % n), 'rb').read()
    rows = 0
    for line in sys.stdin:
        org, owner, ct, nonce, version, generation = line.strip().split('|')
        assert int(version) == current and int(generation) == 1, 'version or generation'
        value = vault.open_envelope(base64.b64decode(ct), base64.b64decode(nonce), keys[current],
                                    org, owner, 'slack', 'delegation', current)
        assert value == plain[org], 'plaintext changed'
        try:  # the new envelope is bound to its key version
            vault.open_envelope(base64.b64decode(ct), base64.b64decode(nonce), keys[current],
                                org, owner, 'slack', 'delegation', current + 1)
        except vault.VaultAccessDenied:
            pass
        else:
            raise AssertionError('key version is not bound in the AAD')
        rows += 1
    assert rows == 2, 'both credentials read back'
FIXTURE
python3 "$temp_root/vault_fixture.py" "$app_dir" seed "$env_file" "$temp_root" |
  psql -X -q -v ON_ERROR_STOP=1 >/dev/null || fail 'vault rotation fixtures'
envelopes() {
  env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT s.org_id, s.owner_id,
      translate(encode(s.ciphertext, 'base64'), E'\n', ''), translate(encode(s.nonce, 'base64'), E'\n', ''),
      s.key_version, s.generation
    FROM company.secrets s JOIN company.organizations o ON o.id = s.org_id
   WHERE o.slug LIKE 'rot-%' AND s.state = 'active' ORDER BY o.slug"
}
rot_versions() {
  env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT string_agg(s.key_version::text, ',')
    FROM company.secrets s JOIN company.organizations o ON o.id = s.org_id
   WHERE o.slug LIKE 'rot-%' AND s.state = 'active'"
}
# The runtime role cannot run the rotation functions.
! psql -X -q -v ON_ERROR_STOP=1 -c 'SELECT * FROM company.vault_key_usage()' >/dev/null 2>&1 || fail 'runtime read vault key usage'
! psql -X -q -v ON_ERROR_STOP=1 -c 'SELECT * FROM company.vault_key_inventory(9, 10)' >/dev/null 2>&1 || fail 'runtime read the vault inventory'
python3 "$vault" rotate "$env_file" "$OAOS_COMPANY_KEY_BACKUP_DIR" > "$temp_root/rotate.out" 2>&1 || { cat "$temp_root/rotate.out" >&2; fail 'vault rotation to V2'; }
grep -Fq 'vault rotation retired: current key version 2, 2 credentials re-encrypted' "$temp_root/rotate.out" || { cat "$temp_root/rotate.out" >&2; fail 'rotation summary'; }
envelopes | python3 "$temp_root/vault_fixture.py" "$app_dir" check "$env_file" "$temp_root" 2 || fail 'rotated envelopes'
grep -Fxq 'OAOS_COMPANY_VAULT_KEY_CURRENT=2' "$env_file" && ! grep -q '^OAOS_COMPANY_VAULT_KEY_V1=' "$env_file" || fail 'V1 retired, V2 current'
[[ $(stat -c %a "$env_file") == 600 ]] || fail 'EnvironmentFile mode after rotation'
rotate_backup=$(find "$OAOS_COMPANY_KEY_BACKUP_DIR" -path '*vault-rotate-*/company.env')
retired_backup=$(find "$OAOS_COMPANY_KEY_BACKUP_DIR" -path '*vault-retired-*/company.env')
grep -q '^OAOS_COMPANY_VAULT_KEY_V1=' "$rotate_backup" && grep -q '^OAOS_COMPANY_VAULT_KEY_V2=' "$rotate_backup" || fail 'new key backed up before re-encryption'
! grep -q '^OAOS_COMPANY_VAULT_KEY_V1=' "$retired_backup" || fail 'retired backup still holds V1'
[[ $(stat -c %a "$rotate_backup") == 600 && $(stat -c %a "$(dirname "$rotate_backup")") == 700 ]] || fail 'key backup modes'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.audit_events
      WHERE event_type = 'personal_credential' AND action = 'rekeyed' AND metadata->>'key_version' = '2'
        AND metadata->>'previous_key_version' = '1' AND NOT metadata ?| array['ciphertext', 'nonce']") == 2 ]] || fail 'rekey audit rows'
! grep -Eq 'rotation-secret|OAOS_COMPANY_VAULT_KEY_V[0-9]+=' "$temp_root/rotate.out" || fail 'rotation printed secret material'
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c05 --read-back > "$temp_root/rotated" || { cat "$temp_root/rotated" >&2; fail 'C05 read-back after rotation'; }
grep -Eq '^Key versions in use +PASS' "$temp_root/rotated" || fail 'key versions read-back row'

# An envelope that no longer opens stops the next rotation before any row
# moves; the old key is kept, and the run resumes once that row is revoked.
read -r bad_org bad_owner < "$temp_root/ids-2"
psql -X -q -v ON_ERROR_STOP=1 >/dev/null <<SQL || fail 'unreadable envelope fixture'
SELECT set_config('company.org_id', '$bad_org', false), set_config('company.member_id', '$bad_owner', false);
SELECT company.rotate_personal_secret(id, decode(repeat('ee', 40), 'hex'), decode(repeat('0f', 12), 'hex'), 2)
  FROM company.secrets WHERE org_id = '$bad_org' AND state = 'active';
SQL
code=0
python3 "$vault" rotate "$env_file" "$OAOS_COMPANY_KEY_BACKUP_DIR" > "$temp_root/rotate-bad.out" 2>&1 || code=$?
[[ $code == 3 ]] && grep -Fq 'Vault key rotation stopped' "$temp_root/rotate-bad.out" || { cat "$temp_root/rotate-bad.out" >&2; fail 'rotation with an unreadable envelope'; }
[[ $(rot_versions) == 2,2 ]] || fail 'failed rotation moved rows'
grep -q '^OAOS_COMPANY_VAULT_KEY_V2=' "$env_file" && grep -Fxq 'OAOS_COMPANY_VAULT_KEY_CURRENT=3' "$env_file" || fail 'old key kept while rotation is incomplete'
psql -X -q -v ON_ERROR_STOP=1 >/dev/null <<SQL || fail 'revoke unreadable envelope'
SELECT set_config('company.org_id', '$bad_org', false), set_config('company.member_id', '$bad_owner', false);
SELECT company.revoke_personal_secret(id) FROM company.secrets WHERE org_id = '$bad_org' AND state = 'active';
SQL
python3 "$vault" rotate "$env_file" "$OAOS_COMPANY_KEY_BACKUP_DIR" > "$temp_root/rotate-resume.out" 2>&1 || { cat "$temp_root/rotate-resume.out" >&2; fail 'resumed rotation'; }
grep -Fq 'vault rotation retired: current key version 3, 1 credentials re-encrypted' "$temp_root/rotate-resume.out" || { cat "$temp_root/rotate-resume.out" >&2; fail 'resumed rotation summary'; }
! grep -q '^OAOS_COMPANY_VAULT_KEY_V2=' "$env_file" || fail 'V2 retired after resume'
[[ $(rot_versions) == 3 ]] || fail 'remaining credential on V3'
# A credential on a key version missing from the EnvironmentFile fails the read-back.
psql -X -q -v ON_ERROR_STOP=1 >/dev/null <<SQL || fail 'unknown key version fixture'
SELECT set_config('company.org_id', '$bad_org', false), set_config('company.member_id', '$bad_owner', false);
SELECT company.store_personal_secret('slack', 'other', decode(repeat('ab', 40), 'hex'), decode(repeat('01', 12), 'hex'), 9);
SQL
code=0
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c05 --read-back > "$temp_root/unknown-version" 2>&1 || code=$?
[[ $code == 1 ]] && grep -Eq '^Key versions in use +FAIL' "$temp_root/unknown-version" || { cat "$temp_root/unknown-version" >&2; fail 'missing key version accepted'; }
psql -X -q -v ON_ERROR_STOP=1 >/dev/null <<SQL || fail 'revoke unknown key version fixture'
SELECT set_config('company.org_id', '$bad_org', false), set_config('company.member_id', '$bad_owner', false);
SELECT company.revoke_personal_secret(id) FROM company.secrets WHERE org_id = '$bad_org' AND state = 'active';
SQL
python3 "$vault" ensure-key "$env_file" || fail 'ensure-key after rotation'
! grep -q '^OAOS_COMPANY_VAULT_KEY_V1=' "$env_file" || fail 'ensure-key brought back a retired key'
printf 'PASS: O5 vault rotation re-encrypts legacy and versioned envelopes, keeps generations, retires old keys only after read-back\n'

# A function drift must fail the same real query path even if the file checksum remains valid.
env -u PGOPTIONS psql -X -q -v ON_ERROR_STOP=1 -c \
  "CREATE OR REPLACE FUNCTION company.personal_secret_use_decision(p_secret uuid, p_assistant uuid, p_generation integer) RETURNS text LANGUAGE sql STABLE SECURITY INVOKER AS 'SELECT ''allow''::text'" >/dev/null
code=0
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c05 --read-back > "$temp_root/drift" 2>&1 || code=$?
[[ $code == 1 ]] || fail 'read-back accepted broken authorization function'
printf 'PASS: real query read-back rejects changed authorization behavior\n'

# O8: every read-back left an evidence JSON (0600 in a 0700 directory) and no
# EnvironmentFile or job password value appears in any of them.
evidence_dir="$HOME/.oaos-install/evidence"
[[ $(stat -c %a "$evidence_dir") == 700 ]] || fail 'evidence directory mode'
[[ $(find "$evidence_dir" -name 'company-c05-*.json' | wc -l) -ge 3 ]] || fail 'c05 evidence files'
! find "$evidence_dir" -type f ! -perm 0600 | grep . >/dev/null || fail 'evidence file modes'
secrets_seen=0
while IFS='=' read -r name value; do
  [[ $name =~ (KEY|SECRET|TOKEN|PASSWORD) && ${#value} -ge 6 ]] || continue
  secrets_seen=$((secrets_seen + 1))
  ! grep -rFq -- "$value" "$evidence_dir" || fail "secret value of $name reached evidence"
done < "$HOME/.config/oaos-company/.env"
((secrets_seen >= 1)) || fail 'no seeded secret to check'
while IFS=: read -r _ _ _ _ value; do
  ! grep -rFq -- "$value" "$evidence_dir" || fail 'job password reached evidence'
done < "$HOME/.config/oaos-company/db-jobs.pgpass"
python3 - "$evidence_dir" <<'PY' || fail 'verified evidence content'
import glob, hashlib, json, os, sys
docs = [json.load(open(p, encoding='utf-8')) for p in glob.glob(os.path.join(sys.argv[1], 'company-c05-*.json'))]
ok = [d for d in docs if d['verify']['exit_code'] == 0 and d['final_state'] == 'verified']
assert ok, 'no verified evidence'
d = ok[0]
assert d['environment'].startswith('E-') and d['checks'] and all(c['status'] == 'PASS' for c in d['checks'])
assert d['summary']['fail'] == 0 and d['manual'] == []
assert hashlib.sha256(d['stdout']['redacted'].encode()).hexdigest() == d['stdout']['sha256']
assert any(x['verify']['exit_code'] != 0 for x in docs), 'failed or manual runs are recorded too'
PY
printf 'PASS: c05 read-back evidence JSON without secrets\n'
