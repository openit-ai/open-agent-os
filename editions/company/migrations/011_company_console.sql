-- C10 closes the required G10 gate for the Company p0 slice:
--   1. the installer creates the real HTTPS setup link,
--   2. the initial administrator is confirmed from a Project DM one-time code,
--   3. member mappings are saved with resumable progress,
--   4. later logins use a separate one-time code and an 8-hour session.
--
-- Only hashes are stored. A plaintext code, session token or CSRF token never
-- reaches PostgreSQL. The trusted server generates the values, sends the code
-- through the Project API and keeps the cookie token outside the database.
-- Console tables use a scope policy but are intentionally not FORCE RLS: the
-- bootstrap functions run before an organization session exists, and the app
-- role is the trusted server described in the C01 contract.

CREATE TABLE company.console_setup (
  org_id uuid PRIMARY KEY REFERENCES company.organizations(id),
  state text NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'admin_created', 'complete')),
  requested_project_user_id text,
  admin_member_id uuid,
  started_at timestamptz NOT NULL DEFAULT now(),
  admin_created_at timestamptz,
  completed_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, admin_member_id) REFERENCES company.members(org_id, id)
);

CREATE TABLE company.console_setup_codes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  project_user_id text NOT NULL CHECK (project_user_id <> ''),
  code_hash char(64) NOT NULL CHECK (code_hash ~ '^[0-9a-f]{64}$'),
  issued_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  request_id text NOT NULL CHECK (request_id <> ''),
  CHECK (expires_at > issued_at)
);
CREATE INDEX console_setup_codes_live
  ON company.console_setup_codes(org_id, project_user_id, issued_at DESC)
  WHERE consumed_at IS NULL;

CREATE TABLE company.console_login_codes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL,
  member_id uuid NOT NULL,
  code_hash char(64) NOT NULL CHECK (code_hash ~ '^[0-9a-f]{64}$'),
  issued_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  request_id text NOT NULL CHECK (request_id <> ''),
  FOREIGN KEY (org_id, member_id) REFERENCES company.members(org_id, id),
  UNIQUE (org_id, id),
  CHECK (expires_at > issued_at)
);
CREATE INDEX console_login_codes_live
  ON company.console_login_codes(org_id, member_id, issued_at DESC)
  WHERE consumed_at IS NULL;

-- 010 (C09) already created console_sessions with a state-based design.
-- 011 adopts the revoked_at design: add the missing columns and backfill.
ALTER TABLE company.console_sessions
  ADD COLUMN IF NOT EXISTS last_seen_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE company.console_sessions
  ADD COLUMN IF NOT EXISTS revoked_at timestamptz;
UPDATE company.console_sessions
   SET revoked_at = now()
 WHERE state = 'revoked' AND revoked_at IS NULL;
DROP INDEX IF EXISTS company.console_sessions_live_member;
CREATE INDEX IF NOT EXISTS console_sessions_live
  ON company.console_sessions(org_id, member_id, expires_at)
  WHERE revoked_at IS NULL;

CREATE TABLE company.console_lockouts (
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  project_user_id text NOT NULL CHECK (project_user_id <> ''),
  failures integer NOT NULL DEFAULT 0 CHECK (failures >= 0),
  window_started_at timestamptz NOT NULL DEFAULT now(),
  locked_until timestamptz,
  PRIMARY KEY (org_id, project_user_id)
);

ALTER TABLE company.console_setup ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.console_setup_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.console_login_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.console_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.console_lockouts ENABLE ROW LEVEL SECURITY;

CREATE POLICY console_setup_scope ON company.console_setup
  USING (org_id::text = current_setting('company.org_id', true))
  WITH CHECK (org_id::text = current_setting('company.org_id', true));
CREATE POLICY console_setup_codes_scope ON company.console_setup_codes
  USING (org_id::text = current_setting('company.org_id', true))
  WITH CHECK (org_id::text = current_setting('company.org_id', true));
CREATE POLICY console_login_codes_scope ON company.console_login_codes
  USING (org_id::text = current_setting('company.org_id', true))
  WITH CHECK (org_id::text = current_setting('company.org_id', true));
CREATE POLICY console_sessions_scope ON company.console_sessions
  USING (org_id::text = current_setting('company.org_id', true))
  WITH CHECK (org_id::text = current_setting('company.org_id', true));
CREATE POLICY console_lockouts_scope ON company.console_lockouts
  USING (org_id::text = current_setting('company.org_id', true))
  WITH CHECK (org_id::text = current_setting('company.org_id', true));

-- Token-bound lookup for the G10 session functions. console_sessions has
-- FORCE RLS (010), so even the SECURITY DEFINER session functions cannot see
-- a row before the org context exists. The functions below set
-- company.console_token_hash to the presented hash first; only the exact row
-- matches, and the hash itself is the unguessable bearer credential.
CREATE POLICY console_sessions_token_lookup ON company.console_sessions
  FOR ALL
  USING (token_hash::text = current_setting('company.console_token_hash', true))
  WITH CHECK (token_hash::text = current_setting('company.console_token_hash', true));

-- G10 runs before a login session exists. The trusted console sets this
-- transaction-local marker only inside the SECURITY DEFINER bootstrap functions
-- so a single-organization install can be discovered before selecting its scope.
CREATE POLICY console_bootstrap_organization_read ON company.organizations
  FOR SELECT USING (current_setting('company.console_bootstrap', true) = 'on');
CREATE POLICY console_bootstrap_member_read ON company.members
  FOR SELECT USING (current_setting('company.console_bootstrap', true) = 'on');
CREATE POLICY console_bootstrap_audit_insert ON company.audit_events
  FOR INSERT WITH CHECK (
    org_id::text = current_setting('company.org_id', true)
    AND current_setting('company.console_bootstrap', true) = 'on'
  );

-- Setup is forward-only. A failed mapping can be retried while the state stays
-- admin_created, but a completed G10 can never be reopened by a later request.
CREATE FUNCTION company.console_setup_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state = 'complete' AND NEW.state <> 'complete' THEN
    RAISE EXCEPTION 'completed console setup is immutable';
  END IF;
  IF OLD.state = 'admin_created' AND NEW.state = 'pending' THEN
    RAISE EXCEPTION 'console setup cannot move back to pending';
  END IF;
  IF NEW.state = 'complete'
     AND (NEW.admin_member_id IS NULL OR NEW.admin_created_at IS NULL) THEN
    RAISE EXCEPTION 'console setup completion requires the initial administrator';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;
CREATE TRIGGER console_setup_forward_only BEFORE UPDATE ON company.console_setup
  FOR EACH ROW EXECUTE FUNCTION company.console_setup_transition();

CREATE FUNCTION company.console_note_failure(p_org uuid, p_project_user_id text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = company, pg_catalog AS $$
BEGIN
  INSERT INTO company.console_lockouts(org_id, project_user_id, failures, window_started_at)
  VALUES (p_org, p_project_user_id, 1, now())
  ON CONFLICT (org_id, project_user_id) DO UPDATE
    SET failures = CASE
          WHEN company.console_lockouts.window_started_at < now() - interval '10 minutes'
            THEN 1 ELSE company.console_lockouts.failures + 1 END,
        window_started_at = CASE
          WHEN company.console_lockouts.window_started_at < now() - interval '10 minutes'
            THEN now() ELSE company.console_lockouts.window_started_at END,
        locked_until = CASE
          WHEN (CASE
                  WHEN company.console_lockouts.window_started_at < now() - interval '10 minutes'
                    THEN 1 ELSE company.console_lockouts.failures + 1 END) >= 5
            THEN now() + interval '15 minutes'
          ELSE company.console_lockouts.locked_until END;
END;
$$;

CREATE FUNCTION company.console_locked(p_org uuid, p_project_user_id text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = company, pg_catalog AS $$
  SELECT EXISTS (
    SELECT 1 FROM company.console_lockouts
     WHERE org_id = p_org AND project_user_id = p_project_user_id
       AND locked_until IS NOT NULL AND locked_until > now()
  );
$$;

CREATE FUNCTION company.console_clear_failures(p_org uuid, p_project_user_id text)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = company, pg_catalog AS $$
  DELETE FROM company.console_lockouts
   WHERE org_id = p_org AND project_user_id = p_project_user_id;
$$;

CREATE FUNCTION company.console_start_setup(p_slug text, p_project_user_id text,
  p_code_hash text, p_ttl interval DEFAULT interval '10 minutes')
RETURNS TABLE(decision text, org_id uuid, expires_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = company, pg_catalog AS $$
DECLARE
  selected_org uuid;
  setup company.console_setup%ROWTYPE;
  deadline timestamptz;
  org_count integer;
BEGIN
  IF p_slug IS NULL OR p_slug !~ '^[a-z0-9][a-z0-9-]{1,62}$'
     OR p_project_user_id IS NULL OR p_project_user_id = ''
     OR p_code_hash IS NULL OR p_code_hash !~ '^[0-9a-f]{64}$'
     OR p_ttl IS NULL OR p_ttl <= interval '0' OR p_ttl > interval '10 minutes' THEN
    RETURN QUERY SELECT 'invalid'::text, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  PERFORM set_config('company.console_bootstrap', 'on', true);
  SELECT count(*) INTO org_count FROM company.organizations;
  IF org_count > 1 THEN
    RETURN QUERY SELECT 'conflict'::text, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  IF org_count = 0 THEN
    selected_org := gen_random_uuid();
    PERFORM set_config('company.org_id', selected_org::text, true);
    INSERT INTO company.organizations(id, slug) VALUES (selected_org, p_slug);
    INSERT INTO company.console_setup(org_id, state) VALUES (selected_org, 'pending');
  ELSE
    SELECT id INTO selected_org FROM company.organizations ORDER BY created_at LIMIT 1;
    PERFORM set_config('company.org_id', selected_org::text, true);
    SELECT s.* INTO setup FROM company.console_setup s
      WHERE s.org_id = selected_org FOR UPDATE;
    IF NOT FOUND THEN
      INSERT INTO company.console_setup(org_id, state) VALUES (selected_org, 'pending');
      SELECT s.* INTO setup FROM company.console_setup s WHERE s.org_id = selected_org;
    ELSIF setup.state = 'complete' THEN
      RETURN QUERY SELECT 'already_complete'::text, selected_org, NULL::timestamptz;
      RETURN;
    ELSIF setup.state = 'admin_created' THEN
      RETURN QUERY SELECT 'admin_exists'::text, selected_org, NULL::timestamptz;
      RETURN;
    END IF;
  END IF;
  IF company.console_locked(selected_org, p_project_user_id) THEN
    RETURN QUERY SELECT 'locked'::text, selected_org, NULL::timestamptz;
    RETURN;
  END IF;
  deadline := now() + p_ttl;
  UPDATE company.console_setup s
     SET requested_project_user_id = p_project_user_id
   WHERE s.org_id = selected_org;
  INSERT INTO company.console_setup_codes(org_id, project_user_id, code_hash,
    expires_at, request_id)
  VALUES (selected_org, p_project_user_id, p_code_hash, deadline,
    gen_random_uuid()::text);
  INSERT INTO company.audit_events(org_id, event_type, action, resource_ref,
    decision, request_id, metadata)
  VALUES (selected_org, 'console_setup', 'code_issued', p_project_user_id,
    'allow', gen_random_uuid()::text, jsonb_build_object('purpose', 'initial_admin'));
  RETURN QUERY SELECT 'issued'::text, selected_org, deadline;
END;
$$;

CREATE FUNCTION company.console_confirm_admin(p_org uuid, p_project_user_id text,
  p_code_hash text)
RETURNS TABLE(decision text, member_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = company, pg_catalog AS $$
DECLARE
  setup company.console_setup%ROWTYPE;
  candidate company.console_setup_codes%ROWTYPE;
  admin_id uuid;
  consumed uuid;
BEGIN
  IF p_org IS NULL OR p_project_user_id IS NULL OR p_project_user_id = ''
     OR p_code_hash IS NULL OR p_code_hash !~ '^[0-9a-f]{64}$' THEN
    RETURN QUERY SELECT 'invalid'::text, NULL::uuid;
    RETURN;
  END IF;
  SELECT * INTO setup FROM company.console_setup WHERE org_id = p_org FOR UPDATE;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'not_started'::text, NULL::uuid;
    RETURN;
  END IF;
  IF setup.state = 'complete' THEN
    RETURN QUERY SELECT 'already_complete'::text, setup.admin_member_id;
    RETURN;
  END IF;
  IF setup.state = 'admin_created' THEN
    RETURN QUERY SELECT 'already_admin'::text, setup.admin_member_id;
    RETURN;
  END IF;
  PERFORM set_config('company.org_id', p_org::text, true);
  PERFORM set_config('company.console_bootstrap', 'on', true);
  IF setup.requested_project_user_id IS DISTINCT FROM p_project_user_id THEN
    PERFORM company.console_note_failure(p_org, p_project_user_id);
    RETURN QUERY SELECT 'invalid'::text, NULL::uuid;
    RETURN;
  END IF;
  IF company.console_locked(p_org, p_project_user_id) THEN
    RETURN QUERY SELECT 'locked'::text, NULL::uuid;
    RETURN;
  END IF;
  SELECT * INTO candidate FROM company.console_setup_codes
   WHERE org_id = p_org AND project_user_id = p_project_user_id
     AND code_hash = p_code_hash AND consumed_at IS NULL
   ORDER BY issued_at DESC LIMIT 1 FOR UPDATE;
  IF NOT FOUND THEN
    PERFORM company.console_note_failure(p_org, p_project_user_id);
    RETURN QUERY SELECT 'invalid'::text, NULL::uuid;
    RETURN;
  END IF;
  IF candidate.expires_at <= now() THEN
    UPDATE company.console_setup_codes SET consumed_at = now() WHERE id = candidate.id;
    PERFORM company.console_note_failure(p_org, p_project_user_id);
    RETURN QUERY SELECT 'expired'::text, NULL::uuid;
    RETURN;
  END IF;
  UPDATE company.console_setup_codes SET consumed_at = now()
   WHERE id = candidate.id AND consumed_at IS NULL AND expires_at > now()
   RETURNING id INTO consumed;
  IF consumed IS NULL THEN
    PERFORM company.console_note_failure(p_org, p_project_user_id);
    RETURN QUERY SELECT 'invalid'::text, NULL::uuid;
    RETURN;
  END IF;
  INSERT INTO company.members(org_id, project_user_id, role, state)
  VALUES (p_org, p_project_user_id, 'admin', 'active')
  RETURNING id INTO admin_id;
  UPDATE company.console_setup
     SET state = 'admin_created', admin_member_id = admin_id,
         admin_created_at = now()
   WHERE org_id = p_org;
  PERFORM company.console_clear_failures(p_org, p_project_user_id);
  INSERT INTO company.audit_events(org_id, actor_id, event_type, action,
    resource_ref, decision, request_id, metadata)
  VALUES (p_org, admin_id, 'console_setup', 'admin_created', admin_id::text,
    'allow', gen_random_uuid()::text, jsonb_build_object('role', 'admin'));
  RETURN QUERY SELECT 'admin_created'::text, admin_id;
END;
$$;

CREATE FUNCTION company.console_add_member(p_org uuid, p_actor uuid,
  p_project_user_id text)
RETURNS TABLE(decision text, member_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = company, pg_catalog AS $$
DECLARE
  setup company.console_setup%ROWTYPE;
  existing company.members%ROWTYPE;
  new_member uuid;
BEGIN
  IF p_org IS NULL OR p_actor IS NULL OR p_project_user_id IS NULL
     OR p_project_user_id = '' OR length(p_project_user_id) > 128 THEN
    RETURN QUERY SELECT 'invalid'::text, NULL::uuid;
    RETURN;
  END IF;
  PERFORM set_config('company.org_id', p_org::text, true);
  PERFORM set_config('company.console_bootstrap', 'on', true);
  SELECT * INTO setup FROM company.console_setup WHERE org_id = p_org FOR UPDATE;
  IF NOT FOUND OR setup.state = 'pending' THEN
    RETURN QUERY SELECT 'not_ready'::text, NULL::uuid;
    RETURN;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM company.members
     WHERE org_id = p_org AND id = p_actor AND role = 'admin' AND state = 'active'
  ) THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid;
    RETURN;
  END IF;
  SELECT * INTO existing FROM company.members
   WHERE org_id = p_org AND project_user_id = p_project_user_id
   ORDER BY created_at LIMIT 1;
  IF FOUND THEN
    IF existing.state = 'active' AND existing.role = 'member' THEN
      RETURN QUERY SELECT 'exists'::text, existing.id;
    ELSE
      RETURN QUERY SELECT 'duplicate'::text, NULL::uuid;
    END IF;
    RETURN;
  END IF;
  INSERT INTO company.members(org_id, project_user_id, role, state)
  VALUES (p_org, p_project_user_id, 'member', 'active')
  RETURNING id INTO new_member;
  INSERT INTO company.audit_events(org_id, actor_id, event_type, action,
    resource_ref, decision, request_id, metadata)
  VALUES (p_org, p_actor, 'console_setup', 'member_mapped', new_member::text,
    'allow', gen_random_uuid()::text,
    jsonb_build_object('project_user_id', p_project_user_id));
  RETURN QUERY SELECT 'created'::text, new_member;
END;
$$;

CREATE FUNCTION company.console_complete_setup(p_org uuid, p_actor uuid)
RETURNS TABLE(decision text, mapped_members integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = company, pg_catalog AS $$
DECLARE
  setup company.console_setup%ROWTYPE;
  total integer;
BEGIN
  PERFORM set_config('company.org_id', p_org::text, true);
  PERFORM set_config('company.console_bootstrap', 'on', true);
  SELECT * INTO setup FROM company.console_setup WHERE org_id = p_org FOR UPDATE;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'not_started'::text, 0;
    RETURN;
  END IF;
  IF setup.state = 'complete' THEN
    SELECT count(*) INTO total FROM company.members
     WHERE org_id = p_org AND role = 'member' AND state = 'active';
    RETURN QUERY SELECT 'already_complete'::text, total;
    RETURN;
  END IF;
  IF setup.state <> 'admin_created' OR setup.admin_member_id IS NULL THEN
    RETURN QUERY SELECT 'not_ready'::text, 0;
    RETURN;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM company.members
     WHERE org_id = p_org AND id = p_actor AND role = 'admin' AND state = 'active'
  ) THEN
    RETURN QUERY SELECT 'denied'::text, 0;
    RETURN;
  END IF;
  SELECT count(*) INTO total FROM company.members
   WHERE org_id = p_org AND role = 'member' AND state = 'active';
  IF total < 2 THEN
    RETURN QUERY SELECT 'incomplete'::text, total;
    RETURN;
  END IF;
  UPDATE company.console_setup
     SET state = 'complete', completed_at = now()
   WHERE org_id = p_org;
  INSERT INTO company.audit_events(org_id, actor_id, event_type, action,
    resource_ref, decision, request_id, metadata)
  VALUES (p_org, p_actor, 'console_setup', 'complete', p_org::text,
    'allow', gen_random_uuid()::text, jsonb_build_object('mapped_members', total));
  RETURN QUERY SELECT 'complete'::text, total;
END;
$$;

CREATE FUNCTION company.console_begin_login(p_project_user_id text, p_code_hash text,
  p_ttl interval DEFAULT interval '10 minutes')
RETURNS TABLE(decision text, org_id uuid, member_id uuid, expires_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = company, pg_catalog AS $$
DECLARE
  candidate RECORD;
  candidate_org uuid;
  match_count integer;
  deadline timestamptz;
BEGIN
  IF p_project_user_id IS NULL OR p_project_user_id = ''
     OR p_code_hash IS NULL OR p_code_hash !~ '^[0-9a-f]{64}$'
     OR p_ttl IS NULL OR p_ttl <= interval '0' OR p_ttl > interval '10 minutes' THEN
    RETURN QUERY SELECT 'invalid'::text, NULL::uuid, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  SELECT count(DISTINCT s.org_id), min(s.org_id::text)::uuid INTO match_count, candidate_org
    FROM company.console_setup s
   WHERE s.state = 'complete';
  IF match_count = 0 THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  IF match_count > 1 THEN
    RETURN QUERY SELECT 'conflict'::text, NULL::uuid, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  PERFORM set_config('company.org_id', candidate_org::text, true);
  PERFORM set_config('company.console_bootstrap', 'on', true);
  SELECT count(*) INTO match_count
    FROM company.members m
    JOIN company.organizations o ON o.id = m.org_id
   WHERE m.org_id = candidate_org AND m.project_user_id = p_project_user_id
     AND m.state = 'active' AND o.state = 'active';
  IF match_count = 0 THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  IF match_count > 1 THEN
    RETURN QUERY SELECT 'conflict'::text, NULL::uuid, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  SELECT m.org_id, m.id AS member_id INTO candidate
    FROM company.members m
    JOIN company.organizations o ON o.id = m.org_id
   WHERE m.org_id = candidate_org AND m.project_user_id = p_project_user_id
     AND m.state = 'active' AND o.state = 'active';
  IF company.console_locked(candidate.org_id, p_project_user_id) THEN
    RETURN QUERY SELECT 'locked'::text, candidate.org_id, candidate.member_id,
      NULL::timestamptz;
    RETURN;
  END IF;
  deadline := now() + p_ttl;
  INSERT INTO company.console_login_codes(org_id, member_id, code_hash,
    expires_at, request_id)
  VALUES (candidate.org_id, candidate.member_id, p_code_hash, deadline,
    gen_random_uuid()::text);
  INSERT INTO company.audit_events(org_id, actor_id, event_type, action,
    resource_ref, decision, request_id)
  VALUES (candidate.org_id, candidate.member_id, 'console_login',
    'code_issued', candidate.member_id::text, 'allow', gen_random_uuid()::text);
  RETURN QUERY SELECT 'issued'::text, candidate.org_id, candidate.member_id, deadline;
END;
$$;

CREATE FUNCTION company.console_open_session(p_org uuid, p_member uuid,
  p_token_hash text, p_csrf_hash text, p_ttl interval DEFAULT interval '8 hours')
RETURNS TABLE(decision text, session_id uuid, expires_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = company, pg_catalog AS $$
DECLARE
  session_id uuid;
  deadline timestamptz;
BEGIN
  IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$'
     OR p_csrf_hash IS NULL OR p_csrf_hash !~ '^[0-9a-f]{64}$'
     OR p_ttl IS NULL OR p_ttl <= interval '0' OR p_ttl > interval '8 hours' THEN
    RETURN QUERY SELECT 'invalid'::text, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  PERFORM set_config('company.org_id', p_org::text, true);
  IF NOT EXISTS (
    SELECT 1 FROM company.members m
    JOIN company.organizations o ON o.id = m.org_id
     WHERE m.org_id = p_org AND m.id = p_member AND m.state = 'active'
       AND o.state = 'active'
  ) THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  deadline := now() + p_ttl;
  INSERT INTO company.console_sessions(org_id, member_id, token_hash, csrf_hash,
    expires_at)
  VALUES (p_org, p_member, p_token_hash, p_csrf_hash, deadline)
  RETURNING id INTO session_id;
  RETURN QUERY SELECT 'created'::text, session_id, deadline;
END;
$$;

CREATE FUNCTION company.console_consume_login_code(p_project_user_id text,
  p_code_hash text, p_token_hash text, p_csrf_hash text,
  p_session_ttl interval DEFAULT interval '8 hours')
RETURNS TABLE(decision text, org_id uuid, member_id uuid, role text,
  expires_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = company, pg_catalog AS $$
DECLARE
  candidate RECORD;
  candidate_org uuid;
  match_count integer;
  code company.console_login_codes%ROWTYPE;
  consumed uuid;
  session_id uuid;
  deadline timestamptz;
BEGIN
  IF p_project_user_id IS NULL OR p_project_user_id = ''
     OR p_code_hash IS NULL OR p_code_hash !~ '^[0-9a-f]{64}$'
     OR p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$'
     OR p_csrf_hash IS NULL OR p_csrf_hash !~ '^[0-9a-f]{64}$'
     OR p_session_ttl IS NULL OR p_session_ttl <= interval '0'
     OR p_session_ttl > interval '8 hours' THEN
    RETURN QUERY SELECT 'invalid'::text, NULL::uuid, NULL::uuid, NULL::text,
      NULL::timestamptz;
    RETURN;
  END IF;
  SELECT count(DISTINCT s.org_id), min(s.org_id::text)::uuid INTO match_count, candidate_org
    FROM company.console_setup s
   WHERE s.state = 'complete';
  IF match_count <> 1 THEN
    RETURN QUERY SELECT CASE WHEN match_count = 0 THEN 'denied' ELSE 'conflict' END::text,
      NULL::uuid, NULL::uuid, NULL::text, NULL::timestamptz;
    RETURN;
  END IF;
  PERFORM set_config('company.org_id', candidate_org::text, true);
  PERFORM set_config('company.console_bootstrap', 'on', true);
  SELECT count(*) INTO match_count
    FROM company.members m
    JOIN company.organizations o ON o.id = m.org_id
   WHERE m.org_id = candidate_org AND m.project_user_id = p_project_user_id
     AND m.state = 'active' AND o.state = 'active';
  IF match_count <> 1 THEN
    RETURN QUERY SELECT CASE WHEN match_count = 0 THEN 'denied' ELSE 'conflict' END::text,
      NULL::uuid, NULL::uuid, NULL::text, NULL::timestamptz;
    RETURN;
  END IF;
  SELECT m.org_id, m.id AS member_id, m.role INTO candidate
    FROM company.members m
    JOIN company.organizations o ON o.id = m.org_id
   WHERE m.org_id = candidate_org AND m.project_user_id = p_project_user_id
     AND m.state = 'active' AND o.state = 'active';
  IF company.console_locked(candidate.org_id, p_project_user_id) THEN
    RETURN QUERY SELECT 'locked'::text, candidate.org_id, candidate.member_id,
      candidate.role, NULL::timestamptz;
    RETURN;
  END IF;
  SELECT c.* INTO code FROM company.console_login_codes c
   WHERE c.org_id = candidate.org_id AND c.member_id = candidate.member_id
     AND c.code_hash = p_code_hash AND c.consumed_at IS NULL
   ORDER BY issued_at DESC LIMIT 1 FOR UPDATE;
  IF NOT FOUND THEN
    PERFORM company.console_note_failure(candidate.org_id, p_project_user_id);
    RETURN QUERY SELECT 'invalid'::text, candidate.org_id, candidate.member_id,
      candidate.role, NULL::timestamptz;
    RETURN;
  END IF;
  IF code.expires_at <= now() THEN
    UPDATE company.console_login_codes SET consumed_at = now() WHERE id = code.id;
    PERFORM company.console_note_failure(candidate.org_id, p_project_user_id);
    RETURN QUERY SELECT 'expired'::text, candidate.org_id, candidate.member_id,
      candidate.role, NULL::timestamptz;
    RETURN;
  END IF;
  UPDATE company.console_login_codes c SET consumed_at = now()
   WHERE c.id = code.id AND c.consumed_at IS NULL AND c.expires_at > now()
   RETURNING c.id INTO consumed;
  IF consumed IS NULL THEN
    PERFORM company.console_note_failure(candidate.org_id, p_project_user_id);
    RETURN QUERY SELECT 'invalid'::text, candidate.org_id, candidate.member_id,
      candidate.role, NULL::timestamptz;
    RETURN;
  END IF;
  deadline := now() + p_session_ttl;
  INSERT INTO company.console_sessions(org_id, member_id, token_hash, csrf_hash,
    expires_at)
  VALUES (candidate.org_id, candidate.member_id, p_token_hash, p_csrf_hash, deadline)
  RETURNING id INTO session_id;
  PERFORM company.console_clear_failures(candidate.org_id, p_project_user_id);
  INSERT INTO company.audit_events(org_id, actor_id, event_type, action,
    resource_ref, decision, request_id)
  VALUES (candidate.org_id, candidate.member_id, 'console_login', 'authenticated',
    session_id::text, 'allow', gen_random_uuid()::text);
  RETURN QUERY SELECT 'authenticated'::text, candidate.org_id, candidate.member_id,
    candidate.role, deadline;
END;
$$;

CREATE FUNCTION company.console_validate_session(p_token_hash text,
  p_csrf_hash text, p_require_csrf boolean)
RETURNS TABLE(decision text, org_id uuid, member_id uuid, role text,
  session_id uuid, expires_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = company, pg_catalog AS $$
DECLARE
  session_row RECORD;
  member_role text;
BEGIN
  IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$' THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::uuid, NULL::text,
      NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  -- Token-bound lookup marker (see console_sessions_token_lookup policy).
  PERFORM set_config('company.console_token_hash', p_token_hash, true);
  SELECT s.id AS session_id, s.org_id, s.member_id, s.csrf_hash, s.expires_at
    INTO session_row
    FROM company.console_sessions s
   WHERE s.token_hash = p_token_hash AND s.revoked_at IS NULL
     AND s.expires_at > now();
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::uuid, NULL::text,
      NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  PERFORM set_config('company.org_id', session_row.org_id::text, true);
  SELECT m.role INTO member_role
    FROM company.members m
    JOIN company.organizations o ON o.id = m.org_id
   WHERE m.org_id = session_row.org_id AND m.id = session_row.member_id
     AND m.state = 'active' AND o.state = 'active';
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::uuid, NULL::text,
      NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  IF p_require_csrf AND (p_csrf_hash IS NULL OR p_csrf_hash <> session_row.csrf_hash) THEN
    RETURN QUERY SELECT 'csrf'::text, session_row.org_id, session_row.member_id, member_role,
      session_row.session_id, session_row.expires_at;
    RETURN;
  END IF;
  RETURN QUERY SELECT 'active'::text, session_row.org_id, session_row.member_id, member_role,
    session_row.session_id, session_row.expires_at;
END;
$$;

CREATE FUNCTION company.console_revoke_session(p_token_hash text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = company, pg_catalog AS $$
DECLARE
  changed uuid;
BEGIN
  IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$' THEN
    RETURN 'invalid';
  END IF;
  -- Token-bound lookup marker (see console_sessions_token_lookup policy).
  PERFORM set_config('company.console_token_hash', p_token_hash, true);
  UPDATE company.console_sessions SET revoked_at = now()
   WHERE token_hash = p_token_hash AND revoked_at IS NULL
   RETURNING id INTO changed;
  IF changed IS NULL THEN RETURN 'absent'; END IF;
  RETURN 'revoked';
END;
$$;

CREATE FUNCTION company.console_setup_status()
RETURNS TABLE(org_id uuid, slug text, state text, admin_member_id uuid,
  active_admins integer, active_members integer, complete boolean,
  next_step text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = company, pg_catalog AS $$
DECLARE
  org_count integer;
  setup company.console_setup%ROWTYPE;
  selected_org company.organizations%ROWTYPE;
BEGIN
  PERFORM set_config('company.console_bootstrap', 'on', true);
  SELECT count(*) INTO org_count FROM company.organizations;
  IF org_count = 0 THEN
    RETURN QUERY SELECT NULL::uuid, NULL::text, 'not_started'::text, NULL::uuid,
      0, 0, false, 'admin'::text;
    RETURN;
  END IF;
  IF org_count > 1 THEN
    RETURN QUERY SELECT NULL::uuid, NULL::text, 'conflict'::text, NULL::uuid,
      0, 0, false, 'resolve'::text;
    RETURN;
  END IF;
  SELECT * INTO selected_org FROM company.organizations ORDER BY created_at LIMIT 1;
  SELECT s.* INTO setup FROM company.console_setup s WHERE s.org_id = selected_org.id;
  IF NOT FOUND THEN
    RETURN QUERY SELECT selected_org.id, selected_org.slug, 'pending'::text,
      NULL::uuid, 0, 0, false, 'admin'::text;
    RETURN;
  END IF;
  RETURN QUERY SELECT selected_org.id, selected_org.slug, setup.state,
    setup.admin_member_id,
    (SELECT count(*)::integer FROM company.members m
      WHERE m.org_id = selected_org.id AND m.role = 'admin' AND m.state = 'active'),
    (SELECT count(*)::integer FROM company.members m
      WHERE m.org_id = selected_org.id AND m.role = 'member' AND m.state = 'active'),
    setup.state = 'complete',
    CASE setup.state WHEN 'pending' THEN 'admin_code'
                    WHEN 'admin_created' THEN 'mappings'
                    ELSE 'login' END::text;
END;
$$;

REVOKE ALL ON FUNCTION company.console_note_failure(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_locked(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_clear_failures(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_start_setup(text, text, text, interval) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_confirm_admin(uuid, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_add_member(uuid, uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_complete_setup(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_begin_login(text, text, interval) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_open_session(uuid, uuid, text, text, interval) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_consume_login_code(text, text, text, text, interval) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_validate_session(text, text, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_revoke_session(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_setup_status() FROM PUBLIC;
