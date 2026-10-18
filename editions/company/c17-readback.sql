-- C17 real SQL responses. Persisted handover evidence is only read; every
-- fixture and audit row created below is discarded with this transaction.
BEGIN;
SELECT set_config('c17.persisted_org', :'org', true);
SELECT set_config('c17.persisted_admin', :'admin', true);
DO $verify$
DECLARE
  persisted_org uuid := current_setting('c17.persisted_org')::uuid;
  persisted_admin uuid := current_setting('c17.persisted_admin')::uuid;
  org_a uuid := gen_random_uuid();
  org_b uuid := gen_random_uuid();
  admin_a uuid := gen_random_uuid();
  owner_a uuid := gen_random_uuid();
  other_a uuid := gen_random_uuid();
  admin_b uuid := gen_random_uuid();
  assistant_a uuid;
  secret_a uuid;
  delegation_a uuid;
  session_a uuid;
  account_a uuid;
  backup_id uuid;
  recovery_id uuid;
  decision text;
  n_sessions integer;
  n_assistants integer;
  n_credentials integer;
  n_delegations integer;
  n_accounts integer;
BEGIN
  -- Persisted evidence: migration 12 is applied, the handover verdict is
  -- verified, the latest backup has a passing recovery drill, and every selected
  -- integration has a confirmed external round-trip.
  PERFORM set_config('company.org_id', persisted_org::text, true);
  PERFORM set_config('company.member_id', persisted_admin::text, true);
  IF (SELECT COALESCE(MAX(version), 0) FROM company.schema_migrations) < 12 THEN
    RAISE EXCEPTION 'migration 12 is not applied';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM company.handover_state h
                  WHERE h.org_id = persisted_org AND h.state = 'verified') THEN
    RAISE EXCEPTION 'persisted handover verdict is not verified';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM company.backup_runs b
                  WHERE b.org_id = persisted_org AND b.state = 'verified') THEN
    RAISE EXCEPTION 'persisted verified backup is missing';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM company.recovery_drills r
                  JOIN company.backup_runs b ON b.org_id = r.org_id AND b.id = r.backup_id
                  WHERE r.org_id = persisted_org AND r.state = 'passed'
                    AND b.state = 'verified') THEN
    RAISE EXCEPTION 'persisted passing recovery drill is missing';
  END IF;
  IF EXISTS (
    SELECT 1 FROM company.integration_settings s
     WHERE s.org_id = persisted_org AND s.state = 'selected'
       AND COALESCE((SELECT h.external_roundtrip->>s.provider
                       FROM company.handover_state h
                      WHERE h.org_id = s.org_id), '') <> 'confirmed') THEN
    RAISE EXCEPTION 'selected integration lacks a confirmed round-trip';
  END IF;

  -- Fixture organization A: one administrator, the departing member and a peer.
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_a, 'c17-a-' || org_a::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (admin_a, org_a, 'c17-admin-' || admin_a::text, 'admin', 'active'),
    (owner_a, org_a, 'c17-owner-' || owner_a::text, 'member', 'active'),
    (other_a, org_a, 'c17-other-' || other_a::text, 'member', 'active');

  -- Backup and recovery evidence are administrator-only.
  SELECT r.backup_id INTO backup_id FROM company.c17_record_backup(
    'manual', '/srv/oaos-company-e/backups/c17-fixture.dump', repeat('a', 64), 4096) r;
  IF backup_id IS NULL THEN RAISE EXCEPTION 'backup evidence was not recorded'; END IF;
  IF (SELECT r.decision FROM company.c17_record_recovery(
        backup_id, 'c17_fixture_restore', false, '{"schema_version":12}'::jsonb) r)
     IS DISTINCT FROM 'recorded' THEN
    RAISE EXCEPTION 'failed recovery drill was not recorded';
  END IF;
  IF (SELECT r.decision FROM company.c17_record_handover(
        'active', 'boot-b', 'boot-a', true, '{}'::jsonb, '{}'::jsonb) r)
     IS DISTINCT FROM 'pending' THEN
    RAISE EXCEPTION 'handover without a passing drill was verified';
  END IF;
  IF (SELECT r.decision FROM company.c17_record_recovery(
        backup_id, 'c17_fixture_restore', true, '{"schema_version":12}'::jsonb) r)
     IS DISTINCT FROM 'recorded' THEN
    RAISE EXCEPTION 'passing recovery drill was not recorded';
  END IF;
  IF (SELECT r.decision FROM company.c17_record_handover(
        'active', 'boot-b', 'boot-a', true, '{}'::jsonb, '{}'::jsonb) r)
     IS DISTINCT FROM 'verified' THEN
    RAISE EXCEPTION 'complete handover evidence was not verified';
  END IF;
  IF (SELECT h.state FROM company.handover_state h WHERE h.org_id = org_a)
     IS DISTINCT FROM 'verified' THEN
    RAISE EXCEPTION 'verified handover state was not persisted';
  END IF;
  -- A reboot with the same boot ID is not survival evidence.
  IF (SELECT r.decision FROM company.c17_record_handover(
        'active', 'boot-a', 'boot-a', true, '{}'::jsonb, '{}'::jsonb) r)
     IS DISTINCT FROM 'verified' THEN
    RAISE EXCEPTION 'a previously verified handover was downgraded';
  END IF;

  -- The departing member owns every capability the revocation procedure covers.
  PERFORM set_config('company.member_id', owner_a::text, true);
  SELECT r.assistant_id INTO assistant_a FROM company.register_personal_assistant() r;
  IF assistant_a IS NULL THEN RAISE EXCEPTION 'fixture assistant was not created'; END IF;
  SELECT r.secret_id INTO secret_a FROM company.store_personal_secret(
    'google_workspace', 'oauth_delegation',
    decode(repeat('11', 32), 'hex'), decode(repeat('22', 12), 'hex'), 1) r;
  IF secret_a IS NULL THEN RAISE EXCEPTION 'fixture credential was not stored'; END IF;

  PERFORM set_config('company.member_id', admin_a::text, true);
  IF (SELECT company.set_integration_state('google_workspace', 'selected', 'fixture'))
     IS DISTINCT FROM 'configured' THEN
    RAISE EXCEPTION 'fixture integration selection failed';
  END IF;
  IF (SELECT r.decision FROM company.configure_integration_app(
        'google_workspace', 'c17-workspace', NULL,
        'https://e.example.test/company/oauth/callback',
        ARRAY['https://www.googleapis.com/auth/gmail.readonly']) r)
     IS DISTINCT FROM 'configured' THEN
    RAISE EXCEPTION 'fixture integration app failed';
  END IF;
  IF (SELECT r.session_id FROM company.console_create_session(
        owner_a, repeat('c', 64), repeat('d', 64), 3600) r) IS NULL THEN
    RAISE EXCEPTION 'fixture console session failed';
  END IF;
  SELECT id INTO session_a FROM company.console_sessions
    WHERE org_id = org_a AND member_id = owner_a AND revoked_at IS NULL;
  IF (SELECT r.account_id FROM company.bind_external_account(
        owner_a, 'slack', 'c17-workspace', 'c17-subject') r) IS NULL THEN
    RAISE EXCEPTION 'fixture external account failed';
  END IF;
  SELECT id INTO account_a FROM company.external_accounts
    WHERE org_id = org_a AND member_id = owner_a AND state = 'active';
  PERFORM set_config('company.member_id', owner_a::text, true);
  SELECT r.delegation_id INTO delegation_a FROM company.bind_delegation(
    'google_workspace', 'c17-subject', 'c17-workspace',
    ARRAY['https://www.googleapis.com/auth/gmail.readonly'], secret_a) r;
  IF delegation_a IS NULL THEN RAISE EXCEPTION 'fixture delegation failed'; END IF;

  -- A non-administrator cannot record evidence or revoke another member.
  PERFORM set_config('company.member_id', owner_a::text, true);
  IF (SELECT r.decision FROM company.c17_record_backup(
        'manual', '/tmp/denied.dump', repeat('b', 64), 1) r)
     IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'member recorded backup evidence';
  END IF;
  IF (SELECT r.decision FROM company.c17_record_recovery(
        backup_id, 'denied_restore', true, '{}'::jsonb) r)
     IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'member recorded recovery evidence';
  END IF;
  IF (SELECT r.decision FROM company.c17_revoke_member(other_a, 'fixture') r)
     IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'member revoked another member';
  END IF;
  IF (SELECT r.decision FROM company.c17_record_handover(
        'active', 'boot-b', 'boot-a', true, '{}'::jsonb, '{}'::jsonb) r)
     IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'member recorded a handover verdict';
  END IF;

  -- Revocation is atomic across sessions, assistants, credentials, delegations
  -- and external accounts, then records the departed member and audit trail.
  PERFORM set_config('company.member_id', admin_a::text, true);
  SELECT r.decision, r.sessions_revoked, r.assistants_revoked,
         r.credentials_revoked, r.delegations_revoked, r.accounts_revoked
    INTO decision, n_sessions, n_assistants, n_credentials, n_delegations, n_accounts
    FROM company.c17_revoke_member(owner_a, 'fixture_departure') r;
  IF decision IS DISTINCT FROM 'revoked' THEN
    RAISE EXCEPTION 'member revocation failed: %', decision;
  END IF;
  IF n_sessions <> 1 OR n_assistants <> 1 OR n_credentials <> 1
     OR n_delegations <> 1 OR n_accounts <> 1 THEN
    RAISE EXCEPTION 'revocation counts were %/%/%/%/%',
      n_sessions, n_assistants, n_credentials, n_delegations, n_accounts;
  END IF;
  IF (SELECT state FROM company.members WHERE id = owner_a) <> 'departed' THEN
    RAISE EXCEPTION 'departing member stayed active';
  END IF;
  -- Assistants and credentials are owner-scoped, so the administrator observes
  -- their state through the C09 metadata mirrors instead of the ciphertext table.
  IF NOT EXISTS (SELECT 1 FROM company.assistant_status a
                  WHERE a.assistant_id = assistant_a AND a.state = 'revoked') THEN
    RAISE EXCEPTION 'assistant stayed active';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM company.secret_status s
                  WHERE s.secret_id = secret_a AND s.state = 'revoked') THEN
    RAISE EXCEPTION 'credential stayed active';
  END IF;
  IF (SELECT state FROM company.integration_delegations WHERE id = delegation_a) <> 'revoked' THEN
    RAISE EXCEPTION 'delegation stayed active';
  END IF;
  IF (SELECT revoked_at FROM company.console_sessions WHERE id = session_a) IS NULL THEN
    RAISE EXCEPTION 'console session stayed active';
  END IF;
  IF (SELECT state FROM company.external_accounts WHERE id = account_a) <> 'revoked' THEN
    RAISE EXCEPTION 'external account stayed active';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM company.member_revocations
                  WHERE org_id = org_a AND member_id = owner_a
                    AND reason = 'fixture_departure' AND sessions_revoked = 1
                    AND assistants_revoked = 1 AND credentials_revoked = 1
                    AND delegations_revoked = 1 AND accounts_revoked = 1) THEN
    RAISE EXCEPTION 'revocation record is missing or incomplete';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM company.audit_events e
                  WHERE e.org_id = org_a AND e.event_type = 'handover'
                    AND e.action = 'member_revoked' AND e.resource_ref = owner_a::text
                    AND e.decision = 'allow') THEN
    RAISE EXCEPTION 'revocation audit event is missing';
  END IF;
  IF EXISTS (SELECT 1 FROM company.audit_events
              WHERE org_id = org_a AND event_type = 'handover'
                AND (metadata ? 'token' OR metadata ? 'secret'
                     OR metadata ? 'ciphertext' OR metadata ? 'api_key')) THEN
    RAISE EXCEPTION 'revocation audit carried credential metadata';
  END IF;
  -- A second call cannot revive or double-count the departed member.
  IF (SELECT r.decision FROM company.c17_revoke_member(owner_a, 'fixture_again') r)
     IS DISTINCT FROM 'already_revoked' THEN
    RAISE EXCEPTION 'departed member was revoked twice';
  END IF;
  IF (SELECT r.decision FROM company.c17_revoke_member(admin_a, 'fixture_last') r)
     IS DISTINCT FROM 'last_admin' THEN
    RAISE EXCEPTION 'last administrator was revoked';
  END IF;

  -- A second organization cannot read or revoke organization A's members.
  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', admin_b::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_b, 'c17-b-' || org_b::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (admin_b, org_b, 'c17-admin-' || admin_b::text, 'admin', 'active');
  IF (SELECT r.decision FROM company.c17_revoke_member(other_a, 'cross_org') r)
     IS DISTINCT FROM 'unregistered' THEN
    RAISE EXCEPTION 'cross-organization revocation was not denied';
  END IF;
  IF (SELECT count(*) FROM company.backup_runs) <> 0 THEN
    RAISE EXCEPTION 'cross-organization backup evidence was visible';
  END IF;
  IF (SELECT count(*) FROM company.handover_state) <> 0 THEN
    RAISE EXCEPTION 'cross-organization handover state was visible';
  END IF;
  IF (SELECT count(*) FROM company.recovery_drills) <> 0 THEN
    RAISE EXCEPTION 'cross-organization recovery evidence was visible';
  END IF;
  IF (SELECT count(*) FROM company.member_revocations) <> 0 THEN
    RAISE EXCEPTION 'cross-organization revocation record was visible';
  END IF;
END;
$verify$;
ROLLBACK;
