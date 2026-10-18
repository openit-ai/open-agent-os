-- C10 uses synthetic fixtures and always rolls back. Only code/token hashes are
-- created; no plaintext credential, DM code or browser token appears here.
BEGIN;
DO $verify$
DECLARE
  suffix text := replace(gen_random_uuid()::text, '-', '');
  admin_project text := 'c10-admin-' || suffix;
  member_one_project text := 'c10-member-one-' || suffix;
  member_two_project text := 'c10-member-two-' || suffix;
  locked_project text := 'c10-locked-' || suffix;
  admin_code text := encode(sha256(convert_to('admin-code-' || suffix, 'UTF8')), 'hex');
  expired_code text := encode(sha256(convert_to('expired-code-' || suffix, 'UTF8')), 'hex');
  member_code text := encode(sha256(convert_to('member-code-' || suffix, 'UTF8')), 'hex');
  expired_login_code text := encode(sha256(convert_to('expired-login-' || suffix, 'UTF8')), 'hex');
  wrong_code text := encode(sha256(convert_to('wrong-code-' || suffix, 'UTF8')), 'hex');
  token_one text := encode(sha256(convert_to('token-one-' || suffix, 'UTF8')), 'hex');
  token_two text := encode(sha256(convert_to('token-two-' || suffix, 'UTF8')), 'hex');
  expired_token text := encode(sha256(convert_to('token-expired-' || suffix, 'UTF8')), 'hex');
  csrf_one text := encode(sha256(convert_to('csrf-one-' || suffix, 'UTF8')), 'hex');
  csrf_two text := encode(sha256(convert_to('csrf-two-' || suffix, 'UTF8')), 'hex');
  csrf_wrong text := encode(sha256(convert_to('csrf-wrong-' || suffix, 'UTF8')), 'hex');
  org uuid;
  admin_id uuid;
  member_one uuid;
  member_two uuid;
  decision text;
  found_id uuid;
  session_decision text;
  mapped integer;
  attempt integer;
  locked_count integer;
  status record;
BEGIN
  SELECT r.decision, r.org_id INTO decision, org
    FROM company.console_start_setup('c10-' || suffix, admin_project,
      admin_code, interval '10 minutes') r;
  IF decision IS DISTINCT FROM 'issued' OR org IS NULL THEN
    RAISE EXCEPTION 'G10 setup code was not issued';
  END IF;
  IF (SELECT count(*) FROM company.console_setup_codes
      WHERE org_id = org AND project_user_id = admin_project
        AND code_hash = admin_code AND consumed_at IS NULL) <> 1 THEN
    RAISE EXCEPTION 'setup code hash was not stored exactly once';
  END IF;

  -- An expired code never creates the administrator and stays consumed.
  INSERT INTO company.console_setup_codes(org_id, project_user_id, code_hash,
    issued_at, expires_at, request_id)
  VALUES (org, admin_project, expired_code, now() - interval '20 minutes',
    now() - interval '10 minutes', gen_random_uuid()::text);
  SELECT r.decision INTO decision
    FROM company.console_confirm_admin(org, admin_project, expired_code) r;
  IF decision IS DISTINCT FROM 'expired' THEN
    RAISE EXCEPTION 'expired setup code was accepted';
  END IF;

  SELECT r.decision, r.member_id INTO decision, admin_id
    FROM company.console_confirm_admin(org, admin_project, admin_code) r;
  IF decision IS DISTINCT FROM 'admin_created' OR admin_id IS NULL THEN
    RAISE EXCEPTION 'initial administrator was not created';
  END IF;
  IF (SELECT state FROM company.console_setup WHERE org_id = org)
     IS DISTINCT FROM 'admin_created' THEN
    RAISE EXCEPTION 'setup state did not advance to admin_created';
  END IF;

  -- One mapping succeeds, a failed retry leaves it intact, and the saved state
  -- can be resumed with the second member.
  SELECT r.decision, r.member_id INTO decision, member_one
    FROM company.console_add_member(org, admin_id, member_one_project) r;
  IF decision IS DISTINCT FROM 'created' OR member_one IS NULL THEN
    RAISE EXCEPTION 'first member mapping was not created';
  END IF;
  SELECT r.decision INTO decision
    FROM company.console_add_member(org, admin_id, '') r;
  IF decision IS DISTINCT FROM 'invalid'
     OR NOT EXISTS (SELECT 1 FROM company.members
                     WHERE org_id = org AND id = member_one AND state = 'active') THEN
    RAISE EXCEPTION 'failed mapping did not preserve the saved member';
  END IF;
  SELECT r.decision, r.member_id INTO decision, found_id
    FROM company.console_add_member(org, admin_id, member_one_project) r;
  IF decision IS DISTINCT FROM 'exists' OR found_id IS DISTINCT FROM member_one THEN
    RAISE EXCEPTION 'duplicate mapping was not idempotent';
  END IF;
  SELECT r.decision, r.mapped_members INTO decision, mapped
    FROM company.console_complete_setup(org, admin_id) r;
  IF decision IS DISTINCT FROM 'incomplete' OR mapped <> 1 THEN
    RAISE EXCEPTION 'G10 completed without two member mappings';
  END IF;
  SELECT r.decision, r.member_id INTO decision, member_two
    FROM company.console_add_member(org, admin_id, member_two_project) r;
  IF decision IS DISTINCT FROM 'created' OR member_two IS NULL THEN
    RAISE EXCEPTION 'second member mapping was not created';
  END IF;
  SELECT r.decision, r.mapped_members INTO decision, mapped
    FROM company.console_complete_setup(org, admin_id) r;
  IF decision IS DISTINCT FROM 'complete' OR mapped <> 2 THEN
    RAISE EXCEPTION 'G10 did not complete with two member mappings';
  END IF;

  SELECT * INTO status FROM company.console_setup_status();
  IF status.state IS DISTINCT FROM 'complete' OR status.complete IS DISTINCT FROM true
     OR status.active_admins <> 1 OR status.active_members <> 2
     OR status.next_step IS DISTINCT FROM 'login' THEN
    RAISE EXCEPTION 'G10 status read-back is inconsistent';
  END IF;

  -- Finished setup cannot be reopened, and a late code is not accepted.
  SELECT r.decision INTO decision
    FROM company.console_start_setup('c10-' || suffix, admin_project,
      admin_code, interval '10 minutes') r;
  IF decision IS DISTINCT FROM 'already_complete' THEN
    RAISE EXCEPTION 'completed G10 accepted a new setup code';
  END IF;
  SELECT r.decision INTO decision
    FROM company.console_confirm_admin(org, admin_project, admin_code) r;
  IF decision IS DISTINCT FROM 'already_complete' THEN
    RAISE EXCEPTION 'completed G10 accepted a new administrator';
  END IF;

  -- Five failures inside ten minutes lock that Project account for fifteen
  -- minutes; login code issuance is refused while the lock is active.
  FOR attempt IN 1..5 LOOP
    SELECT r.decision INTO decision
      FROM company.console_consume_login_code(locked_project, wrong_code,
        encode(sha256(convert_to('locked-token-' || attempt || suffix, 'UTF8')), 'hex'),
        csrf_one, interval '8 hours') r;
    IF decision IS DISTINCT FROM 'denied' THEN
      RAISE EXCEPTION 'unregistered project account was not denied';
    END IF;
  END LOOP;
  SELECT r.decision INTO decision
    FROM company.console_begin_login(member_two_project, member_code,
      interval '10 minutes') r;
  IF decision IS DISTINCT FROM 'issued' THEN
    RAISE EXCEPTION 'registered member login code was not issued';
  END IF;
  -- Lockout is exercised on a real member by writing five bad login attempts.
  DELETE FROM company.console_login_codes WHERE org_id = org;
  DELETE FROM company.console_lockouts WHERE org_id = org;
  FOR attempt IN 1..5 LOOP
    SELECT r.decision INTO decision
      FROM company.console_consume_login_code(member_two_project, wrong_code,
        encode(sha256(convert_to('bad-token-' || attempt || suffix, 'UTF8')), 'hex'),
        csrf_one, interval '8 hours') r;
    IF decision IS DISTINCT FROM 'invalid' THEN
      RAISE EXCEPTION 'bad login code did not fail as invalid';
    END IF;
  END LOOP;
  SELECT count(*) INTO locked_count FROM company.console_lockouts
   WHERE org_id = org AND project_user_id = member_two_project
     AND locked_until IS NOT NULL AND locked_until > now();
  IF locked_count <> 1 THEN
    RAISE EXCEPTION 'five failures did not create a fifteen minute lock';
  END IF;
  SELECT r.decision INTO decision
    FROM company.console_begin_login(member_two_project, member_code,
      interval '10 minutes') r;
  IF decision IS DISTINCT FROM 'locked' THEN
    RAISE EXCEPTION 'locked account received a login code';
  END IF;
  DELETE FROM company.console_lockouts WHERE org_id = org;

  -- A registered member logs in once. The same code cannot be consumed twice.
  SELECT r.decision INTO decision
    FROM company.console_begin_login(member_one_project, member_code,
      interval '10 minutes') r;
  IF decision IS DISTINCT FROM 'issued' THEN
    RAISE EXCEPTION 'member login code was not issued';
  END IF;
  SELECT r.decision INTO decision
    FROM company.console_consume_login_code(member_one_project, wrong_code,
      token_one, csrf_one, interval '8 hours') r;
  IF decision IS DISTINCT FROM 'invalid' THEN
    RAISE EXCEPTION 'wrong login code was accepted';
  END IF;
  SELECT r.decision INTO decision
    FROM company.console_consume_login_code(member_one_project, member_code,
      token_one, csrf_one, interval '8 hours') r;
  IF decision IS DISTINCT FROM 'authenticated' THEN
    RAISE EXCEPTION 'correct login code was rejected';
  END IF;
  SELECT r.decision INTO decision
    FROM company.console_consume_login_code(member_one_project, member_code,
      token_two, csrf_two, interval '8 hours') r;
  IF decision IS DISTINCT FROM 'invalid' THEN
    RAISE EXCEPTION 'consumed login code was accepted twice';
  END IF;

  SELECT r.decision INTO session_decision
    FROM company.console_validate_session(token_one, csrf_one, true) r;
  IF session_decision IS DISTINCT FROM 'active' THEN
    RAISE EXCEPTION 'valid session was not active';
  END IF;
  SELECT r.decision INTO session_decision
    FROM company.console_validate_session(token_one, csrf_wrong, true) r;
  IF session_decision IS DISTINCT FROM 'csrf' THEN
    RAISE EXCEPTION 'CSRF mismatch was accepted';
  END IF;

  -- Expired login codes and expired sessions are denied, and a revoked session
  -- stops working immediately.
  INSERT INTO company.console_login_codes(org_id, member_id, code_hash,
    issued_at, expires_at, request_id)
  VALUES (org, member_one, expired_login_code, now() - interval '20 minutes',
    now() - interval '10 minutes', gen_random_uuid()::text);
  SELECT r.decision INTO decision
    FROM company.console_consume_login_code(member_one_project, expired_login_code,
      token_two, csrf_two, interval '8 hours') r;
  IF decision IS DISTINCT FROM 'expired' THEN
    RAISE EXCEPTION 'expired login code was accepted';
  END IF;
  INSERT INTO company.console_sessions(org_id, member_id, token_hash, csrf_hash,
    created_at, expires_at)
  VALUES (org, member_one, expired_token, csrf_two, now() - interval '9 hours',
    now() - interval '1 hour');
  SELECT r.decision INTO session_decision
    FROM company.console_validate_session(expired_token, csrf_two, false) r;
  IF session_decision IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'expired session was accepted';
  END IF;
  IF company.console_revoke_session(token_one) IS DISTINCT FROM 'revoked' THEN
    RAISE EXCEPTION 'session revocation failed';
  END IF;
  SELECT r.decision INTO session_decision
    FROM company.console_validate_session(token_one, csrf_one, true) r;
  IF session_decision IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'revoked session stayed active';
  END IF;

  PERFORM set_config('company.org_id', org::text, true);
  PERFORM set_config('company.member_id', admin_id::text, true);
  IF (SELECT count(*) FROM company.members
      WHERE org_id = org AND role = 'member' AND state = 'active') <> 2 THEN
    RAISE EXCEPTION 'two member mappings are not present';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM company.audit_events a
     WHERE a.org_id = org AND a.event_type = 'console_setup'
       AND a.action = 'complete' AND a.decision = 'allow'
  ) OR NOT EXISTS (
    SELECT 1 FROM company.audit_events a
     WHERE a.org_id = org AND a.event_type = 'console_login'
       AND a.action = 'authenticated' AND a.decision = 'allow'
  ) THEN
    RAISE EXCEPTION 'console audit trail is incomplete';
  END IF;
  IF EXISTS (
    SELECT 1 FROM company.audit_events a
     WHERE a.org_id = org AND (
       a.metadata ? 'code' OR a.metadata ? 'code_hash' OR a.metadata ? 'token'
       OR a.metadata ? 'csrf' OR a.metadata ? 'password')
  ) THEN
    RAISE EXCEPTION 'console audit metadata contains credential material';
  END IF;
END;
$verify$;
ROLLBACK;
