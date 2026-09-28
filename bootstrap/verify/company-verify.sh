#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"
export OAOS_STATE_EDITION=company
stages=()
for n in $(seq -w 1 17); do stages+=("c$n"); done
export OAOS_STAGES="${stages[*]}"

usage() {
  printf 'Usage: bash bootstrap/verify/company-verify.sh --phase cNN --read-back\nC01 through C03 read-back implemented. Exit: 0 = passed; 3 = prerequisite/manual pending; 1 = failed.\n'
}
phase=''
read_back=0
while (($#)); do
  case $1 in
    --phase)
      (($# >= 2)) || { usage >&2; exit 1; }
      [[ -z $phase && $2 =~ ^c(0[1-9]|1[0-7])$ ]] || { usage >&2; exit 1; }
      phase=$2; shift 2 ;;
    --read-back) read_back=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
done
[[ -n $phase && $read_back == 1 ]] || { usage >&2; exit 1; }
[[ -n ${HOME:-} ]] || { printf 'HOME is required.\n' >&2; exit 1; }
OAOS_STATE_FILE="$(oaos_home)/.oaos-install/company-state.json"
export OAOS_STATE_FILE
if ! state_valid; then die 'Company state file is invalid JSON.'; fi
if [[ $phase == c03 ]]; then
  # shellcheck source=editions/company/c03-verify.sh
  . "$repo_root/editions/company/c03-verify.sh"
  company_c03_verify
  exit $?
fi
if [[ $phase == c02 ]]; then
  # shellcheck source=editions/company/c02-verify.sh
  . "$repo_root/editions/company/c02-verify.sh"
  company_c02_verify
  exit $?
fi
if [[ $phase != c01 ]]; then warn "$phase read-back is not implemented."; exit 3; fi
status=$(stage_status c01)
if [[ $status != applied && $status != verified ]]; then warn 'c01 has not been applied.'; exit 3; fi
if [[ -z ${PGDATABASE:-} ]] || ! have_cmd psql; then warn 'PGDATABASE and psql are required.'; exit 3; fi
export PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
db_query() { psql -X -q -A -t -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null; }
if ! db_query 'SELECT 1' >/dev/null; then warn 'Company database connection failed.'; exit 3; fi
role_safe=$(db_query "SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user") || role_safe=''
if [[ $role_safe != t ]]; then warn 'Read-back needs a non-BYPASSRLS database role.'; exit 3; fi

result=0
bash "$repo_root/editions/company/migrate.sh" --check --through 1 >/dev/null 2>&1 || result=$?
if ((result)); then
  if ((result == 3)); then warn 'Migration version is pending.'; exit 3; fi
  printf '%-3s %-24s %-7s %s\n' '#' CHECK RESULT EVIDENCE
  printf '%-3s %-24s %-7s %s\n' 1 'Migration checksum' FAIL 'Recorded checksum differs or read-back failed'
  printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
  exit 1
fi
version=$(db_query 'SELECT MAX(version) FROM company.schema_migrations') || die 'Cannot read schema version.'
[[ $version =~ ^[0-9]+$ && $version -ge 1 ]] || die 'C01 schema version is absent.'

catalog=$(db_query "SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='company' AND c.relkind='r' AND c.relname IN ('schema_migrations','organizations','members','external_accounts','assistants','secrets','policies','approvals','audit_events','documents','chunks')) || ':' || (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='company' AND t.tgname IN ('assistants_owner_immutable','secrets_owner_immutable','documents_owner_immutable') AND NOT t.tgisinternal) || ':' || (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='company' AND c.relname IN ('members_live_project_user','external_accounts_live_subject','assistants_live_owner','secrets_active_organization','secrets_active_personal'))") || die 'Cannot inspect schema catalog.'
if [[ $catalog != 11:3:5 ]]; then
  printf '%-3s %-24s %-7s %s\n' '#' CHECK RESULT EVIDENCE
  printf '%-3s %-24s %-7s %s\n' 1 'Schema catalog' FAIL 'Expected 11 tables, 3 owner triggers, 5 partial indexes'
  printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
  exit 1
fi

# The transaction always rolls back; fixtures never survive a successful check.
# A failing psql connection also rolls back its open transaction on disconnect.
if ! psql -X -q -v ON_ERROR_STOP=1 >/dev/null 2>&1 <<'SQL'
BEGIN;
DO $verify$
DECLARE
  org_a uuid := gen_random_uuid();
  org_b uuid := gen_random_uuid();
  admin_a uuid := gen_random_uuid();
  owner_a uuid := gen_random_uuid();
  owner_b uuid := gen_random_uuid();
  assistant_a uuid := gen_random_uuid();
  secret_a uuid := gen_random_uuid();
  document_a uuid := gen_random_uuid();
  rejected boolean;
BEGIN
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_a, 'c01-fixture-a-' || org_a::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state)
    VALUES (admin_a, org_a, 'c01-admin-' || admin_a::text, 'admin', 'active'),
           (owner_a, org_a, 'c01-owner-' || owner_a::text, 'member', 'active');
  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', owner_b::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_b, 'c01-fixture-b-' || org_b::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state)
    VALUES (owner_b, org_b, 'c01-owner-' || owner_b::text, 'member', 'active');
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', owner_a::text, true);
  INSERT INTO company.assistants(id, org_id, owner_id) VALUES (assistant_a, org_a, owner_a);
  INSERT INTO company.secrets(id, org_id, owner_id, provider, purpose, ciphertext, nonce, key_version)
    VALUES (secret_a, org_a, owner_a, 'fixture', 'test', decode('01','hex'), decode('02','hex'), 1);
  INSERT INTO company.documents(id, org_id, source, source_id, owner_id, source_acl_version)
    VALUES (document_a, org_a, 'personal', 'c01-' || document_a::text, owner_a, '1');
  INSERT INTO company.chunks(org_id, document_id, ordinal, body) VALUES (org_a, document_a, 0, 'fixture');

  IF (SELECT count(*) FROM company.organizations) <> 1 OR
     (SELECT count(*) FROM company.assistants) <> 1 OR
     (SELECT count(*) FROM company.secrets) <> 1 OR
     (SELECT count(*) FROM company.documents) <> 1 OR
     (SELECT count(*) FROM company.chunks) <> 1 THEN
    RAISE EXCEPTION 'owner baseline is not visible';
  END IF;
  PERFORM set_config('company.member_id', admin_a::text, true);
  IF (SELECT count(*) FROM company.assistants) <> 0 OR
     (SELECT count(*) FROM company.secrets WHERE owner_id IS NOT NULL) <> 0 OR
     (SELECT count(*) FROM company.documents) <> 0 OR
     (SELECT count(*) FROM company.chunks) <> 0 THEN
    RAISE EXCEPTION 'cross-owner row was visible';
  END IF;
  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', owner_b::text, true);
  IF (SELECT count(*) FROM company.organizations) <> 1 OR
     (SELECT count(*) FROM company.members) <> 1 OR
     (SELECT count(*) FROM company.assistants) <> 0 OR
     (SELECT count(*) FROM company.documents) <> 0 THEN
    RAISE EXCEPTION 'cross-organization row was visible';
  END IF;

  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', owner_b::text, true);
  rejected := false;
  BEGIN
    INSERT INTO company.external_accounts(org_id, member_id, provider, workspace_id, subject_id)
      VALUES (org_a, owner_b, 'fixture', 'workspace', 'subject');
  EXCEPTION WHEN foreign_key_violation THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'cross-org FK accepted'; END IF;

  PERFORM set_config('company.member_id', owner_a::text, true);
  rejected := false;
  BEGIN
    INSERT INTO company.assistants(org_id, owner_id) VALUES (org_a, owner_a);
  EXCEPTION WHEN unique_violation THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'assistant partial UNIQUE accepted duplicate'; END IF;
  rejected := false;
  BEGIN
    INSERT INTO company.members(org_id, project_user_id, role, state)
      VALUES (org_a, 'bad-role', 'invalid', 'active');
  EXCEPTION WHEN check_violation THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'member CHECK accepted invalid role'; END IF;

  rejected := false;
  BEGIN
    UPDATE company.assistants SET owner_id = admin_a WHERE id = assistant_a;
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'assistant owner changed'; END IF;
  rejected := false;
  BEGIN
    UPDATE company.secrets SET owner_id = admin_a WHERE id = secret_a;
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'secret owner changed'; END IF;
  rejected := false;
  BEGIN
    UPDATE company.documents SET owner_id = admin_a WHERE id = document_a;
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'document owner changed'; END IF;

  rejected := false;
  PERFORM set_config('company.member_id', admin_a::text, true);
  BEGIN
    UPDATE company.members SET state = 'suspended' WHERE id = admin_a;
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'last active administrator suspended'; END IF;

  PERFORM set_config('company.member_id', owner_a::text, true);
  INSERT INTO company.external_accounts(org_id, member_id, provider, workspace_id, subject_id, state)
    VALUES (org_a, owner_a, 'fixture', 'workspace', 'subject', 'active');
  PERFORM set_config('company.member_id', admin_a::text, true);
  rejected := false;
  BEGIN
    UPDATE company.external_accounts SET member_id = admin_a WHERE member_id = owner_a;
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'external account owner changed'; END IF;
  UPDATE company.members SET state = 'departed' WHERE id = owner_a;
  IF (SELECT count(*) FROM company.external_accounts WHERE member_id = owner_a AND state = 'revoked') <> 1 THEN
    RAISE EXCEPTION 'departed member external account stayed active';
  END IF;
  rejected := false;
  BEGIN
    UPDATE company.members SET state = 'active' WHERE id = owner_a;
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'departed member reactivated'; END IF;
END;
$verify$;
ROLLBACK;
SQL
then
  printf '%-3s %-24s %-7s %s\n' '#' CHECK RESULT EVIDENCE
  printf '%-3s %-24s %-7s %s\n' 1 'Fixture isolation' FAIL 'Transaction fixture or constraint check failed; transaction rolled back'
  printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
  exit 1
fi

printf '%-3s %-24s %-7s %s\n' '#' CHECK RESULT EVIDENCE
printf '%-3s %-24s %-7s %s\n' 1 'Migration checksum' PASS "Version $version matches SQL SHA-256"
printf '%-3s %-24s %-7s %s\n' 2 'Schema catalog' PASS '11 tables, 3 owner triggers, 5 partial indexes'
printf '%-3s %-24s %-7s %s\n' 3 'Organization isolation' PASS 'Cross-organization rows hidden'
printf '%-3s %-24s %-7s %s\n' 4 'Owner isolation' PASS 'Foreign assistant, secret, document and chunk hidden'
printf '%-3s %-24s %-7s %s\n' 5 'Constraint rejection' PASS 'FK, UNIQUE, CHECK and three owner triggers rejected writes'
printf '%-3s %-24s %-7s %s\n' 6 'Member lifecycle' PASS 'Admin and mapping binding protected; departure revoked mapping'
printf '%-3s %-24s %-7s %s\n' 7 'Fixture cleanup' PASS 'Transaction rolled back'
printf 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=0\n'
stage_mark c01 verified 'C01 DB read-back passed; fixture transaction rolled back'
