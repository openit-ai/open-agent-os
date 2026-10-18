-- C09 real SQL responses. Fixtures, sessions and audit rows are discarded with
-- this transaction; no credential material is ever selected.
BEGIN;
DO $verify$
DECLARE
  org_a uuid := gen_random_uuid();
  org_b uuid := gen_random_uuid();
  admin_a uuid := gen_random_uuid();
  member_a uuid := gen_random_uuid();
  member_b uuid := gen_random_uuid();
  admin_b uuid := gen_random_uuid();
  assistant_a uuid := gen_random_uuid();
  assistant_b uuid := gen_random_uuid();
  secret_a uuid := gen_random_uuid();
  secret_b uuid := gen_random_uuid();
  approval_a uuid := gen_random_uuid();
  approval_b uuid := gen_random_uuid();
  session_a uuid := gen_random_uuid();
  session_b uuid := gen_random_uuid();
  session_c uuid := gen_random_uuid();
  session_expired uuid := gen_random_uuid();
  session_revoked uuid := gen_random_uuid();
  created_session uuid;
  snapshot jsonb;
  decision text;
  rows integer;
  hash_admin text := repeat('a', 64);
  hash_member_a text := repeat('b', 64);
  hash_member_b text := repeat('c', 64);
  hash_csrf text := repeat('d', 64);
  hash_expired text := repeat('e', 64);
  hash_revoked text := repeat('f', 64);
  hash_created text := repeat('2', 64);
BEGIN
  -- Organization A: one administrator and two active members.
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_a, 'c09-a-' || org_a::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (admin_a, org_a, 'c09-admin-a', 'admin', 'active'),
    (member_a, org_a, 'c09-member-a', 'member', 'active'),
    (member_b, org_a, 'c09-member-b', 'member', 'active');

  PERFORM set_config('company.member_id', member_a::text, true);
  INSERT INTO company.assistants(id, org_id, owner_id, state)
    VALUES (assistant_a, org_a, member_a, 'active');
  INSERT INTO company.secrets(id, org_id, owner_id, provider, purpose,
    ciphertext, nonce, key_version, state)
    VALUES (secret_a, org_a, member_a, 'google_workspace', 'oauth_delegation',
      decode('deadbeef', 'hex'), decode('c0ffee', 'hex'), 1, 'active');

  PERFORM set_config('company.member_id', member_b::text, true);
  INSERT INTO company.assistants(id, org_id, owner_id, state)
    VALUES (assistant_b, org_a, member_b, 'active');
  INSERT INTO company.secrets(id, org_id, owner_id, provider, purpose,
    ciphertext, nonce, key_version, state)
    VALUES (secret_b, org_a, member_b, 'slack', 'delegation',
      decode('deadbeef', 'hex'), decode('c0ffee', 'hex'), 1, 'active');
  UPDATE company.secrets SET state = 'revoked' WHERE id = secret_b;

  PERFORM set_config('company.member_id', member_a::text, true);
  INSERT INTO company.external_accounts(org_id, member_id, provider,
    workspace_id, subject_id, state)
    VALUES (org_a, member_a, 'slack', 'ws-a', 'subject-a', 'active');
  PERFORM set_config('company.member_id', member_b::text, true);
  INSERT INTO company.external_accounts(org_id, member_id, provider,
    workspace_id, subject_id, state)
    VALUES (org_a, member_b, 'slack', 'ws-b', 'subject-b', 'active'),
           (org_a, member_b, 'notion', 'ws-b', 'subject-b-notion', 'active');

  PERFORM set_config('company.member_id', admin_a::text, true);
  INSERT INTO company.integration_settings(org_id, provider, state, reason, selected_by)
    VALUES (org_a, 'slack', 'selected', NULL, admin_a),
           (org_a, 'notion', 'skip', 'not_selected', admin_a);
  INSERT INTO company.integration_apps(org_id, provider, workspace_id, state, configured_by)
    VALUES (org_a, 'slack', 'ws-org', 'active', admin_a);
  INSERT INTO company.policies(org_id, action, risk, decision, version)
    VALUES (org_a, 'chat.read', 'low', 'allow', 1),
           (org_a, 'mail.send', 'high', 'approval', 1);

  PERFORM set_config('company.member_id', member_a::text, true);
  INSERT INTO company.approvals(id, org_id, requester_id, action, resource_digest,
    context_digest, policy_version, expires_at, state)
    VALUES (approval_a, org_a, member_a, 'mail.send', 'resource-a', 'context-a',
            1, now() + interval '10 minutes', 'pending');
  INSERT INTO company.integration_delegations(org_id, member_id, provider,
    subject_id, workspace_id, scopes, secret_id, state)
    VALUES (org_a, member_a, 'google_workspace', 'subject-a', 'ws-a',
            ARRAY['https://www.googleapis.com/auth/gmail.readonly'], secret_a, 'active');

  PERFORM set_config('company.member_id', member_b::text, true);
  INSERT INTO company.approvals(id, org_id, requester_id, action, resource_digest,
    context_digest, policy_version, expires_at, state)
    VALUES (approval_b, org_a, member_b, 'mail.send', 'resource-b', 'context-b',
            1, now() + interval '10 minutes', 'pending');

  PERFORM set_config('company.member_id', member_a::text, true);
  INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
    decision, reason_code, request_id)
    VALUES (org_a, member_a, member_a, 'policy', 'approval_requested', 'allow',
            NULL, 'c09-request-a');
  PERFORM set_config('company.member_id', member_b::text, true);
  INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
    decision, reason_code, request_id)
    VALUES (org_a, member_b, member_b, 'policy', 'approval_requested', 'allow',
            NULL, 'c09-request-b');
  PERFORM set_config('company.member_id', admin_a::text, true);
  INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
    decision, reason_code, request_id)
    VALUES (org_a, admin_a, NULL, 'backup', 'daily_dump', 'allow',
            'success', 'c09-backup');

  INSERT INTO company.console_sessions(id, org_id, member_id, token_hash,
    csrf_hash, created_at, expires_at, state)
    VALUES (session_a, org_a, admin_a, hash_admin, hash_csrf,
            now(), now() + interval '8 hours', 'active'),
           (session_b, org_a, member_a, hash_member_a, hash_csrf,
            now(), now() + interval '8 hours', 'active'),
           (session_c, org_a, member_b, hash_member_b, hash_csrf,
            now(), now() + interval '8 hours', 'active'),
           (session_expired, org_a, member_a, hash_expired, hash_csrf,
            now() - interval '2 hours', now() - interval '1 hour', 'active'),
           (session_revoked, org_a, member_a, hash_revoked, hash_csrf,
            now(), now() + interval '8 hours', 'revoked');

  -- Organization B must never leak into A's console snapshots.
  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', admin_b::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_b, 'c09-b-' || org_b::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state)
    VALUES (admin_b, org_b, 'c09-admin-b', 'admin', 'active');
  INSERT INTO company.console_sessions(id, org_id, member_id, token_hash,
    csrf_hash, expires_at)
    VALUES (gen_random_uuid(), org_b, admin_b, repeat('1', 64), hash_csrf,
            now() + interval '8 hours');

  -- Administrator sees all four tabs with metadata only.
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  snapshot := company.console_snapshot('members');
  IF snapshot->>'decision' <> 'allow' OR snapshot->>'role' <> 'admin'
     OR snapshot->>'scope' <> 'organization' THEN
    RAISE EXCEPTION 'administrator members snapshot denied';
  END IF;
  IF jsonb_array_length(snapshot->'data'->'members') <> 3 THEN
    RAISE EXCEPTION 'administrator did not see all members';
  END IF;
  IF (SELECT count(*) FROM jsonb_array_elements(snapshot->'data'->'members') e
      WHERE e->>'project_user_id' = 'c09-member-a'
        AND e->>'assistant_state' = 'active'
        AND e->>'secret_status' = 'configured') <> 1 THEN
    RAISE EXCEPTION 'member A assistant or secret status missing';
  END IF;
  IF (SELECT count(*) FROM jsonb_array_elements(snapshot->'data'->'members') e
      WHERE e->>'project_user_id' = 'c09-member-b'
        AND e->>'assistant_state' = 'active') <> 1 THEN
    RAISE EXCEPTION 'member B assistant status missing';
  END IF;
  IF snapshot::text LIKE '%ciphertext%' OR snapshot::text LIKE '%deadbeef%'
     OR snapshot::text LIKE '%' || hash_admin || '%' THEN
    RAISE EXCEPTION 'administrator snapshot exposed credential material';
  END IF;

  snapshot := company.console_snapshot('integrations');
  IF snapshot->>'decision' <> 'allow'
     OR jsonb_array_length(snapshot->'data'->'connections') <> 4 THEN
    RAISE EXCEPTION 'administrator integration snapshot is incomplete';
  END IF;
  IF (SELECT count(*) FROM jsonb_array_elements(snapshot->'data'->'connections') e
      WHERE e->>'provider' = 'slack' AND e->>'selection' = 'selected'
        AND e->>'app_state' = 'active') <> 1 THEN
    RAISE EXCEPTION 'administrator slack selection missing';
  END IF;
  IF (SELECT count(*) FROM jsonb_array_elements(snapshot->'data'->'connections') e
      WHERE e->>'provider' = 'notion' AND e->>'selection' = 'skip'
        AND e->>'app_state' = 'none') <> 1 THEN
    RAISE EXCEPTION 'administrator notion SKIP missing';
  END IF;

  snapshot := company.console_snapshot('policies');
  IF jsonb_array_length(snapshot->'data'->'policies') <> 2
     OR jsonb_array_length(snapshot->'data'->'approvals') <> 2 THEN
    RAISE EXCEPTION 'administrator policy or approval snapshot is incomplete';
  END IF;
  IF snapshot::text LIKE '%resource_digest%' OR snapshot::text LIKE '%context_digest%' THEN
    RAISE EXCEPTION 'policy snapshot exposed approval digests';
  END IF;

  snapshot := company.console_snapshot('status');
  IF (snapshot->'data'->>'migration_version')::integer <> 10 THEN
    RAISE EXCEPTION 'status snapshot migration version is not 10';
  END IF;
  IF jsonb_array_length(snapshot->'data'->'recent_audit') < 1 THEN
    RAISE EXCEPTION 'status snapshot has no audit evidence';
  END IF;
  IF snapshot::text LIKE '%metadata%' THEN
    RAISE EXCEPTION 'status snapshot exposed audit metadata';
  END IF;
  IF snapshot::text NOT LIKE '%c09-backup%' THEN
    RAISE EXCEPTION 'status snapshot backup evidence is missing';
  END IF;

  -- A member sees only their own row, connections, requests and audit events.
  PERFORM set_config('company.member_id', member_a::text, true);
  snapshot := company.console_snapshot('members');
  IF snapshot->>'scope' <> 'self'
     OR jsonb_array_length(snapshot->'data'->'members') <> 1
     OR snapshot->'data'->'members'->0->>'member_id' <> member_a::text THEN
    RAISE EXCEPTION 'member members snapshot leaked another member';
  END IF;
  IF jsonb_array_length(company.console_snapshot('integrations')->'data'->'connections') <> 2 THEN
    RAISE EXCEPTION 'member integration snapshot is not self-scoped';
  END IF;
  snapshot := company.console_snapshot('policies');
  IF jsonb_array_length(snapshot->'data'->'policies') <> 0
     OR jsonb_array_length(snapshot->'data'->'approvals') <> 1
     OR snapshot->'data'->'approvals'->0->>'approval_id' <> approval_a::text THEN
    RAISE EXCEPTION 'member policy snapshot leaked another requester';
  END IF;
  snapshot := company.console_snapshot('status');
  IF snapshot->'data'->>'assistant_state' <> 'active'
     OR snapshot->'data'->>'secret_status' <> 'configured'
     OR (snapshot->'data'->>'pending_approvals')::integer <> 1 THEN
    RAISE EXCEPTION 'member status snapshot is incomplete';
  END IF;
  IF snapshot::text LIKE '%c09-request-b%' OR snapshot::text LIKE '%member-b%' THEN
    RAISE EXCEPTION 'member status snapshot leaked another member';
  END IF;
  IF (SELECT count(*) FROM company.secret_status) <> 1 THEN
    RAISE EXCEPTION 'member secret metadata scope leaked another owner';
  END IF;
  IF (SELECT count(*) FROM company.approvals) <> 1 THEN
    RAISE EXCEPTION 'member approval scope leaked another requester';
  END IF;

  -- The administrator sees organization-wide policy and secret metadata.
  PERFORM set_config('company.member_id', admin_a::text, true);
  IF (SELECT count(*) FROM company.secret_status) <> 2 THEN
    RAISE EXCEPTION 'administrator secret metadata is incomplete';
  END IF;
  IF (SELECT count(*) FROM company.approvals) <> 2 THEN
    RAISE EXCEPTION 'administrator approval read is incomplete';
  END IF;
  IF company.console_snapshot('unknown-tab')->>'decision' <> 'invalid' THEN
    RAISE EXCEPTION 'unknown console tab was accepted';
  END IF;

  -- Token lookup honours expiry and revocation and never returns a plaintext.
  IF (SELECT count(*) FROM company.console_authenticate(hash_admin)) <> 1 THEN
    RAISE EXCEPTION 'administrator session token did not resolve';
  END IF;
  IF (SELECT role FROM company.console_authenticate(hash_member_a)) <> 'member' THEN
    RAISE EXCEPTION 'member session token resolved with the wrong role';
  END IF;
  IF (SELECT count(*) FROM company.console_authenticate(hash_expired)) <> 0 THEN
    RAISE EXCEPTION 'expired console session token resolved';
  END IF;
  IF (SELECT count(*) FROM company.console_authenticate(hash_revoked)) <> 0 THEN
    RAISE EXCEPTION 'revoked console session token resolved';
  END IF;
  IF (SELECT count(*) FROM company.console_authenticate(repeat('0', 64))) <> 0 THEN
    RAISE EXCEPTION 'unknown console session token resolved';
  END IF;

  -- Session creation remains an administrator action; members cannot mint roles.
  PERFORM set_config('company.member_id', member_a::text, true);
  IF (SELECT r.decision FROM company.console_create_session(member_a,
        hash_created, hash_csrf, 3600) r) <> 'denied' THEN
    RAISE EXCEPTION 'member created a console session';
  END IF;
  PERFORM set_config('company.member_id', admin_a::text, true);
  IF (SELECT r.decision FROM company.console_create_session(member_a,
        repeat('z', 64), hash_csrf, 3600) r) <> 'invalid' THEN
    RAISE EXCEPTION 'invalid session hash was accepted';
  END IF;
  SELECT r.session_id INTO created_session FROM company.console_create_session(
    member_a, hash_created, hash_csrf, 3600) r;
  IF created_session IS NULL THEN
    RAISE EXCEPTION 'administrator could not create a console session';
  END IF;
  IF (SELECT count(*) FROM company.console_authenticate(hash_created)) <> 1 THEN
    RAISE EXCEPTION 'created console session did not resolve';
  END IF;
  IF company.console_revoke_session(created_session) <> 'revoked' THEN
    RAISE EXCEPTION 'console session revocation failed';
  END IF;
  IF (SELECT count(*) FROM company.console_authenticate(hash_created)) <> 0 THEN
    RAISE EXCEPTION 'revoked console session still resolved';
  END IF;

  -- Tenant B sees only itself.
  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', admin_b::text, true);
  snapshot := company.console_snapshot('members');
  IF jsonb_array_length(snapshot->'data'->'members') <> 1
     OR snapshot->'data'->'members'->0->>'project_user_id' <> 'c09-admin-b' THEN
    RAISE EXCEPTION 'organization B saw organization A members';
  END IF;
  IF snapshot::text LIKE '%c09-member-a%' THEN
    RAISE EXCEPTION 'organization B snapshot leaked organization A';
  END IF;
END;
$verify$;
ROLLBACK;
