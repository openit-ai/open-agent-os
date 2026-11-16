#!/usr/bin/env bash
# Sourced by company-verify.sh. C01 schema read-back; also the regression check
# every later phase runs. Never prints database output.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/lib/company.sh
. "$repo_root/editions/company/lib/company.sh"

company_c01_db_query() { psql -X -q -A -t -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null; }

company_c01_verify() {
  local status role_safe result=0 version catalog boundary lifecycle pass=0 fail=0
  status=$(stage_status c01)
  if [[ $status != applied && $status != verified ]]; then warn 'c01 has not been applied.'; return 3; fi
  if [[ -z ${PGDATABASE:-} ]] || ! have_cmd psql; then warn 'PGDATABASE and psql are required.'; return 3; fi
  export PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  if ! company_c01_db_query 'SELECT 1' >/dev/null; then warn 'Company database connection failed.'; return 3; fi
  role_safe=$(company_c01_db_query "SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user") || role_safe=''
  if [[ $role_safe != t ]]; then warn 'Read-back needs a non-BYPASSRLS database role.'; return 3; fi

  bash "$repo_root/editions/company/migrate.sh" --check --through 1 >/dev/null 2>&1 || result=$?
  if ((result)); then
    if ((result == 3)); then warn 'Migration version is pending.'; return 3; fi
    printf '%-3s %-24s %-7s %s\n' '#' CHECK RESULT EVIDENCE
    printf '%-3s %-24s %-7s %s\n' 1 'Migration checksum' FAIL 'Recorded checksum differs or read-back failed'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  version=$(company_c01_db_query 'SELECT MAX(version) FROM company.schema_migrations') || die 'Cannot read schema version.'
  [[ $version =~ ^[0-9]+$ && $version -ge 1 ]] || die 'C01 schema version is absent.'

  catalog=$(company_c01_db_query "SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='company' AND c.relkind='r' AND c.relname IN ('schema_migrations','organizations','members','external_accounts','assistants','secrets','policies','approvals','audit_events','documents','chunks')) || ':' || (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='company' AND t.tgname IN ('assistants_owner_immutable','secrets_owner_immutable','documents_owner_immutable') AND NOT t.tgisinternal) || ':' || (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='company' AND c.relname IN ('members_live_project_user','external_accounts_live_subject','assistants_live_owner','secrets_active_organization','secrets_active_personal'))") || die 'Cannot inspect schema catalog.'
  if [[ $catalog != 11:3:5 ]]; then
    printf '%-3s %-24s %-7s %s\n' '#' CHECK RESULT EVIDENCE
    printf '%-3s %-24s %-7s %s\n' 1 'Schema catalog' FAIL 'Expected 11 tables, 3 owner triggers, 5 partial indexes'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi

  # The fixture transaction always rolls back; a failing connection also rolls
  # back its open transaction on disconnect.
  company_readback "$repo_root/editions/company/c01-readback.sql" || true
  boundary=$(company_c01_db_query "SELECT to_regprocedure('company.session_is_org_admin(uuid)') IS NOT NULL") || boundary=''
  lifecycle='Admin and mapping binding protected; departure revoked mapping'
  if [[ $boundary == t ]]; then
    lifecycle+='; member self/cross-member/cross-org writes rejected (019)'
    # With 019 applied the lifecycle check includes the write boundary section.
    company_readback_passed member_write_boundary || COMPANY_READBACK=${COMPANY_READBACK//member_lifecycle/}
  fi

  printf '%-3s %-24s %-7s %s\n' '#' CHECK RESULT EVIDENCE
  printf '%-3s %-24s %-7s %s\n' 1 'Migration checksum' PASS "Version $version matches SQL SHA-256"
  printf '%-3s %-24s %-7s %s\n' 2 'Schema catalog' PASS '11 tables, 3 owner triggers, 5 partial indexes'
  pass=2
  company_readback_line 3 'Organization isolation' organization_isolation 'Cross-organization rows hidden'
  company_readback_line 4 'Owner isolation' owner_isolation 'Foreign assistant, secret, document and chunk hidden'
  company_readback_line 5 'Constraint rejection' constraint_rejection 'FK, UNIQUE, CHECK and three owner triggers rejected writes'
  company_readback_line 6 'Member lifecycle' member_lifecycle "$lifecycle"
  company_readback_line 7 'Fixture cleanup' rolled_back 'Transaction rolled back'
  printf 'Summary: PASS=%s FAIL=%s MANUAL=0 SKIP=0\n' "$pass" "$fail"
  ((fail == 0)) || return 1
  stage_mark c01 verified 'C01 DB read-back passed; fixture transaction rolled back'
}
