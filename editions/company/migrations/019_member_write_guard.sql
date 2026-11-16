-- oaos-applied-with: 1
-- SPDX-License-Identifier: BUSL-1.1
-- Company edition: see LICENSE-COMPANY at the repository root.
--
-- Member write boundary (company-design 10, O1). Applied by C03 and every later
-- phase through the maintenance path as the NOLOGIN Company owner role.
--
-- Migration 001 created one FOR ALL policy on company.members that only checked
-- the organization, so any session of the organization could INSERT, UPDATE or
-- DELETE any member row: a member could make itself an administrator, reactivate
-- a suspended colleague or rewrite another member's Project mapping.
--
-- Now:
-- - SELECT stays organization scoped (directory, mapping lookups);
-- - INSERT/UPDATE/DELETE need the session member (company.member_id) to be an
--   active administrator of that organization, or run as the Company owner role
--   (only reachable through SECURITY DEFINER functions that check the caller
--   themselves, and through the maintenance path);
-- - the only other INSERT is the bootstrap of an organization without any
--   member row: the session member creates itself as its first active
--   administrator (fixtures and G10 run through this or the owner path);
-- - nobody changes their own role or state, administrators included; another
--   administrator (or a checked SECURITY DEFINER procedure) has to do it. The
--   last-administrator trigger from 001 still applies on top.

CREATE FUNCTION company.session_runs_as_owner() RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
  SELECT EXISTS (
    SELECT 1 FROM pg_namespace n
     WHERE n.nspname = 'company'
       AND n.nspowner = (SELECT r.oid FROM pg_roles r WHERE r.rolname = current_user)
  );
$$;

-- Evaluated by the members policies. A function keeps the self-referencing
-- check out of the policy expression (direct recursion is rejected by
-- PostgreSQL) while the lookup itself still runs under the caller's RLS.
CREATE FUNCTION company.session_is_org_admin(p_org uuid) RETURNS boolean
LANGUAGE plpgsql STABLE SET search_path = company, pg_catalog AS $$
BEGIN
  IF p_org IS NULL OR p_org::text IS DISTINCT FROM current_setting('company.org_id', true) THEN
    RETURN false;
  END IF;
  RETURN EXISTS (
    SELECT 1 FROM company.members m
     WHERE m.org_id = p_org
       AND m.id::text = current_setting('company.member_id', true)
       AND m.role = 'admin' AND m.state = 'active'
  );
END;
$$;

CREATE FUNCTION company.member_bootstrap_allowed(p_org uuid, p_id uuid, p_role text,
  p_state text) RETURNS boolean
LANGUAGE plpgsql STABLE SET search_path = company, pg_catalog AS $$
BEGIN
  IF p_org IS NULL OR p_org::text IS DISTINCT FROM current_setting('company.org_id', true)
     OR p_id IS NULL OR p_id::text IS DISTINCT FROM current_setting('company.member_id', true)
     OR p_role IS DISTINCT FROM 'admin' OR p_state IS DISTINCT FROM 'active' THEN
    RETURN false;
  END IF;
  RETURN NOT EXISTS (SELECT 1 FROM company.members m WHERE m.org_id = p_org);
END;
$$;

DROP POLICY member_scope ON company.members;
CREATE POLICY member_read ON company.members FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true));
CREATE POLICY member_insert ON company.members FOR INSERT
  WITH CHECK (org_id::text = current_setting('company.org_id', true) AND (
    company.session_runs_as_owner() OR company.session_is_org_admin(org_id)
    OR company.member_bootstrap_allowed(org_id, id, role, state)));
CREATE POLICY member_update ON company.members FOR UPDATE
  USING (org_id::text = current_setting('company.org_id', true) AND (
    company.session_runs_as_owner() OR company.session_is_org_admin(org_id)))
  WITH CHECK (org_id::text = current_setting('company.org_id', true) AND (
    company.session_runs_as_owner() OR company.session_is_org_admin(org_id)));
CREATE POLICY member_delete ON company.members FOR DELETE
  USING (org_id::text = current_setting('company.org_id', true) AND (
    company.session_runs_as_owner() OR company.session_is_org_admin(org_id)));

CREATE FUNCTION company.reject_self_member_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = company, pg_catalog AS $$
BEGIN
  IF OLD.id::text = current_setting('company.member_id', true)
     AND (NEW.role IS DISTINCT FROM OLD.role OR NEW.state IS DISTINCT FROM OLD.state)
     AND NOT company.session_runs_as_owner() THEN
    RAISE EXCEPTION 'a member cannot change their own role or state';
  END IF;
  RETURN NEW;
END;
$$;
-- Sorted before members_c05_revoke_capabilities (014), which temporarily
-- impersonates the target member while it revokes capabilities.
CREATE TRIGGER members_a_self_change_guard BEFORE UPDATE OF role, state ON company.members
  FOR EACH ROW EXECUTE FUNCTION company.reject_self_member_change();

REVOKE ALL ON FUNCTION company.session_runs_as_owner() FROM PUBLIC;
REVOKE ALL ON FUNCTION company.session_is_org_admin(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.member_bootstrap_allowed(uuid, uuid, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION company.reject_self_member_change() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION company.session_runs_as_owner() TO oaos_company_runtime;
GRANT EXECUTE ON FUNCTION company.session_is_org_admin(uuid) TO oaos_company_runtime;
GRANT EXECUTE ON FUNCTION company.member_bootstrap_allowed(uuid, uuid, text, text)
  TO oaos_company_runtime;
