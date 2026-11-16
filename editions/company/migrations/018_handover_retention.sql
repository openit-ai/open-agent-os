-- oaos-applied-with: 12
-- C17 handover hardening and retention. Applied by C17 and every later phase.
--   P1-4  c17_revoke_member returned early for a departed member and revoked
--         assistants/credentials only while the member was still active;
--   P1-10 c17_evidence_safe checked only top-level, case-sensitive keys;
--   P2-13 180-day audit retention: maintenance-only purge with its own audit row;
--   P2-14 30-day deletion of personal content of departed members.

-- 1. Evidence uses the recursive, case-insensitive C07 key check (migration 015).
CREATE OR REPLACE FUNCTION company.c17_evidence_safe(p_evidence jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT COALESCE(p_evidence, '{}'::jsonb) = '{}'::jsonb
    OR (jsonb_typeof(p_evidence) = 'object'
        AND company.c07_sensitive_key(p_evidence) IS NULL);
$$;

-- 2. Revocation independent of the member's current state. Owner-scoped rows
--    of an active member are revoked under that member's context (as before);
--    a suspended member's were already revoked by the migration 014 trigger on
--    the transition away from active. Sessions, delegations and external
--    accounts are revoked in every state. One revocation record per member.
CREATE OR REPLACE FUNCTION company.c17_revoke_member(p_member_id uuid, p_reason text)
RETURNS TABLE(decision text, sessions_revoked integer, assistants_revoked integer,
  credentials_revoked integer, delegations_revoked integer, accounts_revoked integer)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE
  org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
  member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
  target company.members%ROWTYPE;
  other_admins integer;
  n_sessions integer := 0;
  n_assistants integer := 0;
  n_credentials integer := 0;
  n_delegations integer := 0;
  n_accounts integer := 0;
BEGIN
  IF NOT company.c03_admin_ready() OR org IS NULL OR member IS NULL
     OR p_member_id IS NULL
     OR p_reason IS NULL OR p_reason = '' OR length(p_reason) > 256 THEN
    RETURN QUERY SELECT 'denied'::text, 0, 0, 0, 0, 0;
    RETURN;
  END IF;
  SELECT m.* INTO target FROM company.members m
    WHERE m.org_id = org AND m.id = p_member_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'unregistered'::text, 0, 0, 0, 0, 0;
    RETURN;
  END IF;
  IF EXISTS (SELECT 1 FROM company.member_revocations r
              WHERE r.org_id = org AND r.member_id = target.id) THEN
    RETURN QUERY SELECT 'already_revoked'::text, 0, 0, 0, 0, 0;
    RETURN;
  END IF;
  IF target.role = 'admin' AND target.state = 'active' THEN
    SELECT count(*) INTO other_admins FROM company.members m
      WHERE m.org_id = org AND m.role = 'admin' AND m.state = 'active'
        AND m.id <> target.id;
    IF other_admins = 0 THEN
      RETURN QUERY SELECT 'last_admin'::text, 0, 0, 0, 0, 0;
      RETURN;
    END IF;
  END IF;

  UPDATE company.console_sessions s SET state = 'revoked', revoked_at = now()
    WHERE s.org_id = org AND s.member_id = target.id AND s.revoked_at IS NULL;
  GET DIAGNOSTICS n_sessions = ROW_COUNT;
  UPDATE company.integration_delegations d SET state = 'revoked', updated_at = now()
    WHERE d.org_id = org AND d.member_id = target.id AND d.state = 'active';
  GET DIAGNOSTICS n_delegations = ROW_COUNT;
  UPDATE company.external_accounts e SET state = 'revoked'
    WHERE e.org_id = org AND e.member_id = target.id AND e.state <> 'revoked';
  GET DIAGNOSTICS n_accounts = ROW_COUNT;

  IF target.state = 'active' THEN
    PERFORM set_config('company.member_id', target.id::text, true);
    SELECT count(*) INTO n_credentials FROM company.secrets s
      WHERE s.org_id = org AND s.owner_id = target.id AND s.state = 'active';
    UPDATE company.assistants a SET state = 'revoked'
      WHERE a.org_id = org AND a.owner_id = target.id AND a.state = 'active';
    GET DIAGNOSTICS n_assistants = ROW_COUNT;
    PERFORM set_config('company.member_id', member::text, true);
  END IF;

  IF target.state <> 'departed' THEN
    UPDATE company.members m SET state = 'departed', updated_at = now()
      WHERE m.org_id = org AND m.id = target.id;
  END IF;

  INSERT INTO company.member_revocations(org_id, member_id, reason,
    sessions_revoked, assistants_revoked, credentials_revoked,
    delegations_revoked, accounts_revoked, performed_by)
  VALUES (org, target.id, p_reason, n_sessions, n_assistants, n_credentials,
    n_delegations, n_accounts, member);

  INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
    resource_ref, decision, reason_code, request_id, metadata)
  VALUES (org, member, target.id, 'handover', 'member_revoked', target.id::text,
    'allow', p_reason, company.request_id(),
    jsonb_build_object('sessions', n_sessions, 'assistants', n_assistants,
      'credentials', n_credentials, 'delegations', n_delegations,
      'accounts', n_accounts, 'previous_state', target.state));

  RETURN QUERY SELECT 'revoked'::text, n_sessions, n_assistants,
    n_credentials, n_delegations, n_accounts;
END;
$$;

-- 3. Retention. Runs only through the local postgres maintenance account (the
--    oaos-company-maintenance timer), never as the runtime role: audit rows are
--    append-only for the application. Each run writes its own audit row per
--    organization with the cutoff and the number of rows removed.
CREATE FUNCTION company.maintenance_purge(p_audit_days integer DEFAULT 180,
  p_personal_days integer DEFAULT 30)
RETURNS TABLE(org_id uuid, audit_deleted bigint, documents_deleted bigint,
  knowledge_deleted bigint, outputs_deleted bigint)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE
  org record;
  departed record;
  audit_cutoff timestamptz;
  personal_cutoff timestamptz;
  n_audit bigint;
  n_docs bigint;
  n_knowledge bigint;
  n_outputs bigint;
  step bigint;
BEGIN
  IF NOT COALESCE((SELECT r.rolsuper FROM pg_roles r WHERE r.rolname = current_user), false) THEN
    RAISE EXCEPTION 'retention purge runs only through the maintenance account';
  END IF;
  IF p_audit_days IS NULL OR p_audit_days < 180
     OR p_personal_days IS NULL OR p_personal_days < 30 THEN
    RAISE EXCEPTION 'retention windows may not be shorter than the design minimum';
  END IF;
  audit_cutoff := now() - make_interval(days => p_audit_days);
  personal_cutoff := now() - make_interval(days => p_personal_days);
  FOR org IN SELECT o.id FROM company.organizations o ORDER BY o.id LOOP
    n_docs := 0; n_knowledge := 0; n_outputs := 0;
    -- Personal content of members who departed at least p_personal_days ago.
    FOR departed IN
      SELECT m.id FROM company.members m
        LEFT JOIN company.member_revocations r
          ON r.org_id = m.org_id AND r.member_id = m.id
       WHERE m.org_id = org.id AND m.state = 'departed'
         AND COALESCE(r.created_at, m.updated_at) < personal_cutoff
    LOOP
      PERFORM set_config('company.org_id', org.id::text, true);
      PERFORM set_config('company.member_id', departed.id::text, true);
      DELETE FROM company.documents d
        WHERE d.org_id = org.id AND d.owner_id = departed.id AND d.source = 'personal';
      GET DIAGNOSTICS step = ROW_COUNT; n_docs := n_docs + step;
      DELETE FROM company.assistant_knowledge k
        WHERE k.org_id = org.id AND k.owner_id = departed.id;
      GET DIAGNOSTICS step = ROW_COUNT; n_knowledge := n_knowledge + step;
      DELETE FROM company.assistant_outputs o
        WHERE o.org_id = org.id AND o.owner_id = departed.id;
      GET DIAGNOSTICS step = ROW_COUNT; n_outputs := n_outputs + step;
    END LOOP;
    DELETE FROM company.audit_events e
      WHERE e.org_id = org.id AND e.at < audit_cutoff;
    GET DIAGNOSTICS n_audit = ROW_COUNT;
    PERFORM set_config('company.org_id', org.id::text, true);
    PERFORM set_config('company.member_id', '', true);
    INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
      resource_ref, decision, reason_code, request_id, metadata)
    VALUES (org.id, NULL, NULL, 'maintenance', 'retention_purged', org.id::text,
      'allow', 'retention', company.request_id(),
      jsonb_build_object('audit_cutoff', audit_cutoff, 'audit_deleted', n_audit,
        'personal_cutoff', personal_cutoff, 'documents_deleted', n_docs,
        'knowledge_deleted', n_knowledge, 'outputs_deleted', n_outputs));
    org_id := org.id; audit_deleted := n_audit; documents_deleted := n_docs;
    knowledge_deleted := n_knowledge; outputs_deleted := n_outputs;
    RETURN NEXT;
  END LOOP;
END;
$$;
REVOKE EXECUTE ON FUNCTION company.maintenance_purge(integer, integer)
  FROM PUBLIC, oaos_company_runtime;
