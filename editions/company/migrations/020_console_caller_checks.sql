-- oaos-applied-with: 11
-- SPDX-License-Identifier: BUSL-1.1
-- Company edition: see LICENSE-COMPANY at the repository root.
--
-- Console caller checks (company-design 10, O6). Applied by C10 and every
-- later phase through the maintenance path as the NOLOGIN Company owner role.
--
-- The 15 SECURITY DEFINER functions of migration 011 run as the owner role.
-- Review result per function:
-- - console_start_setup, console_confirm_admin, console_begin_login,
--   console_consume_login_code: pre-login; the caller is identified only by the
--   one-time code (hash) they present. Unchanged.
-- - console_validate_session, console_revoke_session: identified by the session
--   token hash (the bearer credential). Unchanged.
-- - console_setup_status: returns counts and the next step only. Unchanged.
-- - console_note_failure, console_locked, console_clear_failures: internal
--   helpers that trusted their arguments. EXECUTE is now revoked from the
--   runtime role; only the definer functions above call them.
-- - console_add_member, console_complete_setup: trusted p_actor/p_org from the
--   argument list. They now require the session context (company.org_id and
--   company.member_id, set by the console after console_validate_session) to
--   be exactly that organization and administrator.
-- - console_open_session: minted a session for any active member without a
--   code. It now only opens the first session of the initial administrator
--   while G10 is in admin_created (the confirm step); every other login goes
--   through console_consume_login_code.
-- In addition:
-- - the console_bootstrap marker policies (organizations/members read, audit
--   insert) only apply inside the owner-role definer functions; a runtime
--   session that sets the marker itself no longer reads other organizations;
-- - console_sessions and console_login_codes rows can only be written for the
--   session member itself or by an active administrator of the organization
--   (or by the definer functions), so a member cannot store a login code or a
--   session token for an administrator and log in as them.

-- 1. Bootstrap marker: owner-role definer functions only.
DROP POLICY console_bootstrap_organization_read ON company.organizations;
DROP POLICY console_bootstrap_member_read ON company.members;
DROP POLICY console_bootstrap_audit_insert ON company.audit_events;
CREATE POLICY console_bootstrap_organization_read ON company.organizations
  FOR SELECT USING (current_setting('company.console_bootstrap', true) = 'on'
    AND company.session_runs_as_owner());
CREATE POLICY console_bootstrap_member_read ON company.members
  FOR SELECT USING (current_setting('company.console_bootstrap', true) = 'on'
    AND company.session_runs_as_owner());
CREATE POLICY console_bootstrap_audit_insert ON company.audit_events
  FOR INSERT WITH CHECK (
    org_id::text = current_setting('company.org_id', true)
    AND current_setting('company.console_bootstrap', true) = 'on'
    AND company.session_runs_as_owner()
  );

-- 2. Internal helpers.
REVOKE ALL ON FUNCTION company.console_note_failure(uuid, text)
  FROM PUBLIC, oaos_company_runtime;
REVOKE ALL ON FUNCTION company.console_locked(uuid, text)
  FROM PUBLIC, oaos_company_runtime;
REVOKE ALL ON FUNCTION company.console_clear_failures(uuid, text)
  FROM PUBLIC, oaos_company_runtime;

-- 3. Caller-bound wrappers. The checked 011 bodies stay as *_unchecked
--    (owner-only) so their behaviour is unchanged once the caller is accepted.
ALTER FUNCTION company.console_add_member(uuid, uuid, text)
  RENAME TO console_add_member_unchecked;
ALTER FUNCTION company.console_complete_setup(uuid, uuid)
  RENAME TO console_complete_setup_unchecked;
ALTER FUNCTION company.console_open_session(uuid, uuid, text, text, interval)
  RENAME TO console_open_session_unchecked;
REVOKE ALL ON FUNCTION company.console_add_member_unchecked(uuid, uuid, text)
  FROM PUBLIC, oaos_company_runtime;
REVOKE ALL ON FUNCTION company.console_complete_setup_unchecked(uuid, uuid)
  FROM PUBLIC, oaos_company_runtime;
REVOKE ALL ON FUNCTION company.console_open_session_unchecked(uuid, uuid, text, text, interval)
  FROM PUBLIC, oaos_company_runtime;

CREATE FUNCTION company.console_session_is(p_org uuid, p_member uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
  SELECT p_org IS NOT NULL AND p_member IS NOT NULL
     AND p_org::text = current_setting('company.org_id', true)
     AND p_member::text = current_setting('company.member_id', true);
$$;

CREATE FUNCTION company.console_add_member(p_org uuid, p_actor uuid,
  p_project_user_id text)
RETURNS TABLE(decision text, member_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = company, pg_catalog AS $$
BEGIN
  IF NOT company.console_session_is(p_org, p_actor) THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid;
    RETURN;
  END IF;
  RETURN QUERY SELECT * FROM company.console_add_member_unchecked(p_org, p_actor,
    p_project_user_id);
END;
$$;

CREATE FUNCTION company.console_complete_setup(p_org uuid, p_actor uuid)
RETURNS TABLE(decision text, mapped_members integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = company, pg_catalog AS $$
BEGIN
  IF NOT company.console_session_is(p_org, p_actor) THEN
    RETURN QUERY SELECT 'denied'::text, 0;
    RETURN;
  END IF;
  RETURN QUERY SELECT * FROM company.console_complete_setup_unchecked(p_org, p_actor);
END;
$$;

CREATE FUNCTION company.console_open_session(p_org uuid, p_member uuid,
  p_token_hash text, p_csrf_hash text, p_ttl interval DEFAULT interval '8 hours')
RETURNS TABLE(decision text, session_id uuid, expires_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = company, pg_catalog AS $$
BEGIN
  IF p_org IS NULL OR p_member IS NULL THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  PERFORM set_config('company.org_id', p_org::text, true);
  IF NOT EXISTS (
    SELECT 1 FROM company.console_setup s
     WHERE s.org_id = p_org AND s.state = 'admin_created'
       AND s.admin_member_id = p_member
  ) THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::timestamptz;
    RETURN;
  END IF;
  RETURN QUERY SELECT * FROM company.console_open_session_unchecked(p_org, p_member,
    p_token_hash, p_csrf_hash, p_ttl);
END;
$$;

REVOKE ALL ON FUNCTION company.console_session_is(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_add_member(uuid, uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_complete_setup(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.console_open_session(uuid, uuid, text, text, interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION company.console_session_is(uuid, uuid) TO oaos_company_runtime;
GRANT EXECUTE ON FUNCTION company.console_add_member(uuid, uuid, text) TO oaos_company_runtime;
GRANT EXECUTE ON FUNCTION company.console_complete_setup(uuid, uuid) TO oaos_company_runtime;
GRANT EXECUTE ON FUNCTION company.console_open_session(uuid, uuid, text, text, interval)
  TO oaos_company_runtime;

-- 4. Login codes and sessions belong to the session member, unless an active
--    administrator (C09 console_create_session) or a definer function writes.
CREATE FUNCTION company.console_credential_write_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = company, pg_catalog AS $$
BEGIN
  IF company.session_runs_as_owner() THEN
    RETURN NEW;
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.member_id IS DISTINCT FROM OLD.member_id THEN
    RAISE EXCEPTION 'console credential owner is immutable';
  END IF;
  IF NEW.member_id::text IS DISTINCT FROM current_setting('company.member_id', true)
     AND NOT company.session_is_org_admin(NEW.org_id) THEN
    RAISE EXCEPTION 'console credentials belong to the session member';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER console_sessions_write_guard BEFORE INSERT OR UPDATE ON company.console_sessions
  FOR EACH ROW EXECUTE FUNCTION company.console_credential_write_guard();
CREATE TRIGGER console_login_codes_write_guard BEFORE INSERT OR UPDATE ON company.console_login_codes
  FOR EACH ROW EXECUTE FUNCTION company.console_credential_write_guard();
REVOKE ALL ON FUNCTION company.console_credential_write_guard() FROM PUBLIC;
