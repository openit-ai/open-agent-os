-- C09 minimal management console. The console is a single loopback process
-- behind the existing nginx `/company/` path. It never stores or returns a
-- plaintext credential: session cookies are hashed, and the Vault exposes only
-- metadata presence (`configured`/`none`/`revoked`) to the console.
--
-- The trusted application sets company.org_id and company.member_id only after
-- it authenticated a Project member. The browser cannot choose either value.

CREATE TABLE company.console_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  member_id uuid NOT NULL,
  token_hash char(64) NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  csrf_hash char(64) NOT NULL CHECK (csrf_hash ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'revoked')),
  FOREIGN KEY (org_id, member_id) REFERENCES company.members(org_id, id),
  UNIQUE (org_id, id),
  CHECK (expires_at > created_at)
);
CREATE INDEX console_sessions_live_member
  ON company.console_sessions(org_id, member_id, expires_at)
  WHERE state = 'active';

-- Metadata-only mirror of the Secret Vault. C09 needs to tell an administrator
-- that a credential exists without granting access to the ciphertext. The
-- mirror is maintained by a trigger and contains no envelope material.
CREATE TABLE company.secret_status (
  secret_id uuid PRIMARY KEY,
  org_id uuid NOT NULL,
  owner_id uuid,
  provider text NOT NULL,
  purpose text NOT NULL,
  state text NOT NULL CHECK (state IN ('active', 'revoked')),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, secret_id) REFERENCES company.secrets(org_id, id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, owner_id) REFERENCES company.members(org_id, id)
);
CREATE INDEX secret_status_owner
  ON company.secret_status(org_id, owner_id, state);

INSERT INTO company.secret_status(secret_id, org_id, owner_id, provider, purpose, state, updated_at)
SELECT s.id, s.org_id, s.owner_id, s.provider, s.purpose, s.state, now()
  FROM company.secrets s;

CREATE FUNCTION company.c09_sync_secret_status() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    DELETE FROM company.secret_status WHERE secret_id = OLD.id;
    RETURN OLD;
  END IF;
  PERFORM set_config('company.secret_status_sync', 'on', true);
  INSERT INTO company.secret_status(secret_id, org_id, owner_id, provider, purpose, state, updated_at)
  VALUES (NEW.id, NEW.org_id, NEW.owner_id, NEW.provider, NEW.purpose, NEW.state, now())
  ON CONFLICT (secret_id) DO UPDATE
    SET state = EXCLUDED.state, provider = EXCLUDED.provider,
        purpose = EXCLUDED.purpose, updated_at = now();
  RETURN NEW;
END;
$$;
CREATE TRIGGER secrets_console_status
  AFTER INSERT OR UPDATE OF state, provider, purpose OR DELETE ON company.secrets
  FOR EACH ROW EXECUTE FUNCTION company.c09_sync_secret_status();

-- Metadata-only assistant state for the console. C04 keeps the assistants table
-- owner-scoped, so administrators read this mirror instead of the assistant row.
CREATE TABLE company.assistant_status (
  assistant_id uuid PRIMARY KEY,
  org_id uuid NOT NULL,
  owner_id uuid NOT NULL,
  state text NOT NULL CHECK (state IN ('active', 'suspended', 'revoked')),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, assistant_id) REFERENCES company.assistants(org_id, id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, owner_id) REFERENCES company.members(org_id, id)
);
CREATE INDEX assistant_status_owner
  ON company.assistant_status(org_id, owner_id, state);

INSERT INTO company.assistant_status(assistant_id, org_id, owner_id, state, updated_at)
SELECT a.id, a.org_id, a.owner_id, a.state, now()
  FROM company.assistants a;

CREATE FUNCTION company.c09_sync_assistant_status() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    DELETE FROM company.assistant_status WHERE assistant_id = OLD.id;
    RETURN OLD;
  END IF;
  PERFORM set_config('company.assistant_status_sync', 'on', true);
  INSERT INTO company.assistant_status(assistant_id, org_id, owner_id, state, updated_at)
  VALUES (NEW.id, NEW.org_id, NEW.owner_id, NEW.state, now())
  ON CONFLICT (assistant_id) DO UPDATE
    SET state = EXCLUDED.state, owner_id = EXCLUDED.owner_id, updated_at = now();
  RETURN NEW;
END;
$$;
CREATE TRIGGER assistants_console_status
  AFTER INSERT OR UPDATE OF state, owner_id OR DELETE ON company.assistants
  FOR EACH ROW EXECUTE FUNCTION company.c09_sync_assistant_status();

-- An active member can use the console. The administrator role adds the
-- organization-wide tabs; a member is always limited to their own rows.
CREATE FUNCTION company.c09_member_ready() RETURNS boolean
LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT EXISTS (
    SELECT 1 FROM company.members m
    JOIN company.organizations o ON o.id = m.org_id
    WHERE m.org_id::text = current_setting('company.org_id', true)
      AND m.id::text = current_setting('company.member_id', true)
      AND m.state = 'active' AND o.state = 'active'
  );
$$;

-- Code lookup is the authentication step. The caller sets this transaction
-- value to the SHA-256 of the cookie token before reading a session row.
CREATE FUNCTION company.console_authenticate(p_token_hash text)
RETURNS TABLE(session_id uuid, org_id uuid, member_id uuid, role text,
              csrf_hash text, expires_at timestamptz)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE session_org uuid;
DECLARE session_member uuid;
BEGIN
  IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$' THEN
    RETURN;
  END IF;
  PERFORM set_config('company.console_token_hash', p_token_hash, true);
  SELECT s.org_id, s.member_id INTO session_org, session_member
    FROM company.console_sessions s
   WHERE s.token_hash = p_token_hash
     AND s.state = 'active' AND s.expires_at > now();
  IF NOT FOUND THEN
    RETURN;
  END IF;
  -- Set the authenticated context only after the token lookup. The normal RLS
  -- policies then resolve this member and organization without recursive
  -- policy evaluation.
  PERFORM set_config('company.org_id', session_org::text, true);
  PERFORM set_config('company.member_id', session_member::text, true);
  RETURN QUERY
    SELECT s.id, s.org_id, s.member_id, m.role, s.csrf_hash::text, s.expires_at
      FROM company.console_sessions s
      JOIN company.members m ON m.org_id = s.org_id AND m.id = s.member_id
      JOIN company.organizations o ON o.id = s.org_id
     WHERE s.token_hash = p_token_hash
       AND s.state = 'active' AND s.expires_at > now()
       AND m.state = 'active' AND o.state = 'active'
       AND s.org_id = session_org AND s.member_id = session_member;
END;
$$;

-- C10 creates sessions after the Project one-time-code login. Keeping creation
-- administrator-only means C09 cannot be used to mint a role by itself.
CREATE FUNCTION company.console_create_session(p_member_id uuid,
  p_token_hash text, p_csrf_hash text, p_ttl_seconds integer)
RETURNS TABLE(decision text, session_id uuid, expires_at timestamptz)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
DECLARE member_state text;
DECLARE created company.console_sessions%ROWTYPE;
BEGIN
  IF NOT company.c03_admin_ready() OR org IS NULL THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$'
     OR p_csrf_hash IS NULL OR p_csrf_hash !~ '^[0-9a-f]{64}$'
     OR p_ttl_seconds IS NULL OR p_ttl_seconds < 60 OR p_ttl_seconds > 28800 THEN
    RETURN QUERY SELECT 'invalid'::text, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  SELECT m.state INTO member_state FROM company.members m
    WHERE m.org_id = org AND m.id = p_member_id;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'unregistered'::text, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  IF member_state <> 'active' THEN
    RETURN QUERY SELECT 'inactive'::text, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  BEGIN
    INSERT INTO company.console_sessions(org_id, member_id, token_hash,
      csrf_hash, expires_at)
    VALUES (org, p_member_id, p_token_hash, p_csrf_hash,
            now() + make_interval(secs => p_ttl_seconds))
    RETURNING * INTO created;
  EXCEPTION WHEN unique_violation THEN
    RETURN QUERY SELECT 'duplicate'::text, NULL::uuid, NULL::timestamptz;
    RETURN;
  END;
  RETURN QUERY SELECT 'created'::text, created.id, created.expires_at;
END;
$$;

CREATE FUNCTION company.console_revoke_session(p_session_id uuid) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
DECLARE member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
DECLARE member_role text;
DECLARE affected integer;
BEGIN
  IF NOT company.c09_member_ready() OR org IS NULL OR member IS NULL THEN
    RETURN 'denied';
  END IF;
  SELECT m.role INTO member_role FROM company.members m
    WHERE m.org_id = org AND m.id = member;
  IF member_role = 'admin' THEN
    UPDATE company.console_sessions SET state = 'revoked'
      WHERE id = p_session_id AND org_id = org AND state = 'active';
  ELSE
    UPDATE company.console_sessions SET state = 'revoked'
      WHERE id = p_session_id AND org_id = org AND member_id = member
        AND state = 'active';
  END IF;
  GET DIAGNOSTICS affected = ROW_COUNT;
  IF affected > 0 THEN RETURN 'revoked'; END IF;
  RETURN 'denied';
END;
$$;

-- One read path for all four tabs. Each tab returns role-scoped JSON and never
-- includes a ciphertext, nonce, token, document body, prompt or audit metadata.
CREATE FUNCTION company.console_snapshot(p_tab text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
DECLARE
  org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
  member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
  member_role text;
  member_scope text;
  data jsonb;
BEGIN
  IF NOT company.c09_member_ready() OR org IS NULL OR member IS NULL THEN
    RETURN jsonb_build_object('decision', 'denied', 'tab', p_tab,
      'role', NULL, 'scope', 'none', 'data', '{}'::jsonb);
  END IF;
  SELECT m.role INTO member_role FROM company.members m
    WHERE m.org_id = org AND m.id = member;
  member_scope := CASE member_role WHEN 'admin' THEN 'organization' ELSE 'self' END;

  IF p_tab = 'members' THEN
    IF member_role = 'admin' THEN
      SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'member_id', m.id,
        'project_user_id', m.project_user_id,
        'role', m.role,
        'state', m.state,
        'assistant_state', COALESCE((
          SELECT a.state FROM company.assistant_status a
           WHERE a.org_id = m.org_id AND a.owner_id = m.id
             AND a.state <> 'revoked'
           ORDER BY a.assistant_id LIMIT 1), 'none'),
        'secret_status', CASE
          WHEN EXISTS (SELECT 1 FROM company.secret_status ss
                        WHERE ss.org_id = m.org_id AND ss.owner_id = m.id
                          AND ss.state = 'active') THEN 'configured'
          WHEN EXISTS (SELECT 1 FROM company.secret_status ss
                        WHERE ss.org_id = m.org_id AND ss.owner_id = m.id
                          AND ss.state = 'revoked') THEN 'revoked'
          ELSE 'none' END
      ) ORDER BY m.created_at, m.id), '[]'::jsonb)
      INTO data
      FROM company.members m
      WHERE m.org_id = org;
    ELSE
      SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'member_id', m.id,
        'project_user_id', m.project_user_id,
        'role', m.role,
        'state', m.state,
        'assistant_state', COALESCE((
          SELECT a.state FROM company.assistant_status a
           WHERE a.org_id = m.org_id AND a.owner_id = m.id
             AND a.state <> 'revoked'
           ORDER BY a.assistant_id LIMIT 1), 'none'),
        'secret_status', CASE
          WHEN EXISTS (SELECT 1 FROM company.secret_status ss
                        WHERE ss.org_id = m.org_id AND ss.owner_id = m.id
                          AND ss.state = 'active') THEN 'configured'
          WHEN EXISTS (SELECT 1 FROM company.secret_status ss
                        WHERE ss.org_id = m.org_id AND ss.owner_id = m.id
                          AND ss.state = 'revoked') THEN 'revoked'
          ELSE 'none' END
      ) ORDER BY m.created_at, m.id), '[]'::jsonb)
      INTO data
      FROM company.members m
      WHERE m.org_id = org AND m.id = member;
    END IF;
    data := jsonb_build_object('members', data);

  ELSIF p_tab = 'integrations' THEN
    IF member_role = 'admin' THEN
      SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'provider', p.provider,
        'selection', COALESCE(s.state, 'skip'),
        'selection_reason', CASE WHEN COALESCE(s.state, 'skip') = 'skip'
          THEN COALESCE(s.reason, 'not_selected') ELSE NULL END,
        'app_state', COALESCE((
          SELECT a.state FROM company.integration_apps a
           WHERE a.org_id = org AND a.provider = p.provider
           ORDER BY a.configured_at DESC LIMIT 1), 'none'),
        'app_configured', EXISTS (
          SELECT 1 FROM company.integration_apps a
           WHERE a.org_id = org AND a.provider = p.provider
             AND a.state = 'active'),
        'active_connections', (
          SELECT count(*) FROM company.external_accounts e
           WHERE e.org_id = org AND e.provider = p.provider
             AND e.state = 'active'),
        'revoked_connections', (
          SELECT count(*) FROM company.external_accounts e
           WHERE e.org_id = org AND e.provider = p.provider
             AND e.state = 'revoked')
      ) ORDER BY p.provider), '[]'::jsonb)
      INTO data
      FROM unnest(ARRAY['slack', 'notion', 'google_workspace', 'microsoft_365'])
        AS p(provider)
      LEFT JOIN company.integration_settings s
        ON s.org_id = org AND s.provider = p.provider;
    ELSE
      SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'provider', x.provider,
        'connection', x.kind,
        'state', x.state,
        'secret_status', CASE
          WHEN EXISTS (SELECT 1 FROM company.secret_status ss
                        WHERE ss.org_id = org AND ss.owner_id = member
                          AND ss.provider = x.provider AND ss.state = 'active')
            THEN 'configured'
          WHEN EXISTS (SELECT 1 FROM company.secret_status ss
                        WHERE ss.org_id = org AND ss.owner_id = member
                          AND ss.provider = x.provider AND ss.state = 'revoked')
            THEN 'revoked'
          ELSE 'none' END
      ) ORDER BY x.provider, x.kind), '[]'::jsonb)
      INTO data
      FROM (
        SELECT e.provider, 'external_account'::text AS kind, e.state
          FROM company.external_accounts e
         WHERE e.org_id = org AND e.member_id = member
        UNION ALL
        SELECT d.provider, 'delegation'::text, d.state
          FROM company.integration_delegations d
         WHERE d.org_id = org AND d.member_id = member
      ) x;
    END IF;
    data := jsonb_build_object('connections', data);

  ELSIF p_tab = 'policies' THEN
    IF member_role = 'admin' THEN
      SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'action', p.action,
        'risk', p.risk,
        'decision', p.decision,
        'version', p.version
      ) ORDER BY p.action), '[]'::jsonb)
      INTO data
      FROM company.policies p
      WHERE p.org_id = org;
      data := jsonb_build_object('policies', data, 'approvals',
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
          'approval_id', a.id,
          'requester_id', a.requester_id,
          'approver_id', a.approver_id,
          'action', a.action,
          'policy_version', a.policy_version,
          'expires_at', a.expires_at,
          'state', a.state
        ) ORDER BY a.expires_at, a.id)
        FROM company.approvals a
        WHERE a.org_id = org
          AND a.state IN ('pending', 'approved', 'denied', 'expired', 'consumed')), '[]'::jsonb));
    ELSE
      data := jsonb_build_object('policies', '[]'::jsonb, 'approvals',
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
          'approval_id', a.id,
          'requester_id', a.requester_id,
          'approver_id', a.approver_id,
          'action', a.action,
          'policy_version', a.policy_version,
          'expires_at', a.expires_at,
          'state', a.state
        ) ORDER BY a.expires_at, a.id)
        FROM company.approvals a
        WHERE a.org_id = org AND a.requester_id = member), '[]'::jsonb));
    END IF;

  ELSIF p_tab = 'status' THEN
    IF member_role = 'admin' THEN
      data := jsonb_build_object(
        'organization_state', (SELECT o.state FROM company.organizations o
                                WHERE o.id = org),
        'migration_version', (SELECT COALESCE(MAX(version), 0)
                               FROM company.schema_migrations),
        'active_members', (SELECT count(*) FROM company.members m
                            WHERE m.org_id = org AND m.state = 'active'),
        'active_assistants', (SELECT count(*) FROM company.assistant_status a
                               WHERE a.org_id = org AND a.state = 'active'),
        'pending_approvals', (SELECT count(*) FROM company.approvals a
                               WHERE a.org_id = org AND a.state = 'pending'),
        'documents', (SELECT count(*) FROM company.documents d
                       WHERE d.org_id = org AND d.deleted_at IS NULL),
        'chunks', (SELECT count(*) FROM company.chunks c
                    WHERE c.org_id = org),
        'audit_events', (SELECT count(*) FROM company.audit_events e
                          WHERE e.org_id = org),
        'last_backup_at', (SELECT MAX(e.at) FROM company.audit_events e
                            WHERE e.org_id = org
                              AND e.event_type IN ('backup', 'restore')),
        'recent_audit', COALESCE((
          SELECT jsonb_agg(jsonb_build_object(
            'at', e.at, 'event_type', e.event_type, 'action', e.action,
            'decision', e.decision, 'reason_code', e.reason_code,
            'request_id', e.request_id)
            ORDER BY e.at DESC)
          FROM (SELECT * FROM company.audit_events
                 WHERE org_id = org ORDER BY at DESC LIMIT 20) e), '[]'::jsonb));
    ELSE
      data := jsonb_build_object(
        'assistant_state', COALESCE((
          SELECT a.state FROM company.assistant_status a
           WHERE a.org_id = org AND a.owner_id = member
             AND a.state <> 'revoked'
           ORDER BY a.assistant_id LIMIT 1), 'none'),
        'secret_status', CASE
          WHEN EXISTS (SELECT 1 FROM company.secret_status ss
                        WHERE ss.org_id = org AND ss.owner_id = member
                          AND ss.state = 'active') THEN 'configured'
          WHEN EXISTS (SELECT 1 FROM company.secret_status ss
                        WHERE ss.org_id = org AND ss.owner_id = member
                          AND ss.state = 'revoked') THEN 'revoked'
          ELSE 'none' END,
        'connections', (SELECT count(*) FROM company.external_accounts e
                         WHERE e.org_id = org AND e.member_id = member
                           AND e.state = 'active'),
        'pending_approvals', (SELECT count(*) FROM company.approvals a
                               WHERE a.org_id = org AND a.requester_id = member
                                 AND a.state = 'pending'),
        'recent_audit', COALESCE((
          SELECT jsonb_agg(jsonb_build_object(
            'at', e.at, 'event_type', e.event_type, 'action', e.action,
            'decision', e.decision, 'reason_code', e.reason_code,
            'request_id', e.request_id)
            ORDER BY e.at DESC)
          FROM (SELECT * FROM company.audit_events
                 WHERE org_id = org
                   AND (actor_id = member OR owner_id = member)
                 ORDER BY at DESC LIMIT 20) e), '[]'::jsonb));
    END IF;

  ELSE
    RETURN jsonb_build_object('decision', 'invalid', 'tab', p_tab,
      'role', member_role, 'scope', 'none', 'data', '{}'::jsonb);
  END IF;

  RETURN jsonb_build_object('decision', 'allow', 'tab', p_tab,
    'role', member_role, 'scope', member_scope, 'data', data);
END;
$$;

-- Token-bound reads resolve only the session that owns the presented hash.
CREATE POLICY console_session_token_read ON company.console_sessions FOR SELECT
  USING (state = 'active' AND expires_at > now()
         AND token_hash::text = current_setting('company.console_token_hash', true));
CREATE POLICY console_session_self_read ON company.console_sessions FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true)
         AND member_id::text = current_setting('company.member_id', true));
CREATE POLICY console_session_admin_all ON company.console_sessions FOR ALL
  USING (org_id::text = current_setting('company.org_id', true)
         AND company.c03_admin_ready())
  WITH CHECK (org_id::text = current_setting('company.org_id', true)
              AND company.c03_admin_ready());
CREATE POLICY console_session_self_revoke ON company.console_sessions FOR UPDATE
  USING (org_id::text = current_setting('company.org_id', true)
         AND member_id::text = current_setting('company.member_id', true))
  WITH CHECK (org_id::text = current_setting('company.org_id', true)
              AND member_id::text = current_setting('company.member_id', true)
              AND state = 'revoked');

CREATE POLICY assistant_status_owner_read ON company.assistant_status FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true)
         AND owner_id::text = current_setting('company.member_id', true));
CREATE POLICY assistant_status_admin_read ON company.assistant_status FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true)
         AND company.c03_admin_ready());
CREATE POLICY assistant_status_sync_insert ON company.assistant_status FOR INSERT
  WITH CHECK (org_id::text = current_setting('company.org_id', true)
              AND current_setting('company.assistant_status_sync', true) = 'on');
CREATE POLICY assistant_status_sync_update ON company.assistant_status FOR UPDATE
  USING (org_id::text = current_setting('company.org_id', true)
         AND current_setting('company.assistant_status_sync', true) = 'on')
  WITH CHECK (org_id::text = current_setting('company.org_id', true));

CREATE POLICY secret_status_owner_read ON company.secret_status FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true)
         AND (owner_id IS NULL
              OR owner_id::text = current_setting('company.member_id', true)));
CREATE POLICY secret_status_admin_read ON company.secret_status FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true)
         AND company.c03_admin_ready());
CREATE POLICY secret_status_sync_insert ON company.secret_status FOR INSERT
  WITH CHECK (org_id::text = current_setting('company.org_id', true)
              AND current_setting('company.secret_status_sync', true) = 'on');
CREATE POLICY secret_status_sync_update ON company.secret_status FOR UPDATE
  USING (org_id::text = current_setting('company.org_id', true)
         AND current_setting('company.secret_status_sync', true) = 'on')
  WITH CHECK (org_id::text = current_setting('company.org_id', true));

ALTER TABLE company.console_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.console_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE company.secret_status ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.secret_status FORCE ROW LEVEL SECURITY;
ALTER TABLE company.assistant_status ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.assistant_status FORCE ROW LEVEL SECURITY;

-- Administrators may read all organization approvals; members keep the C01
-- requester-only scope. C07's JIT workflow can consume the same rows.
CREATE POLICY approval_admin_read ON company.approvals FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true)
         AND company.c03_admin_ready());

