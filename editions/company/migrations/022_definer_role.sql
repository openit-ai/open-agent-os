-- oaos-applied-with: 11
-- SPDX-License-Identifier: BUSL-1.1
-- Company edition: see LICENSE-COMPANY at the repository root.
--
-- Definer functions without DDL rights (company-design 10, O6). Applied by C10
-- and every later phase through the maintenance path as the NOLOGIN Company
-- owner role.
--
-- The SECURITY DEFINER console functions of 011/020 ran as the owner role,
-- which owns the schema and every table: a flaw in one of them could ALTER or
-- DROP objects, disable RLS or create new functions. They now belong to
-- oaos_company_definer (created by db-roles.sql): NOLOGIN, no CREATE on the
-- schema or the database, not a member of the owner role, holding only the
-- table privileges below. Ownership itself is moved by db-definers.sql right
-- after this migration (changing a function owner to a role without CREATE on
-- the schema needs the local postgres maintenance account, not the owner role).
--
-- Because the definer role does not own the tables, row level security now
-- applies to it on every table:
-- - FORCE RLS tables (organizations, members, audit_events, console_sessions)
--   already applied their policies to the owner; session_runs_as_owner() below
--   also accepts the definer role, so the 019/020 owner-path policy branches
--   behave exactly as before;
-- - the four console tables without FORCE RLS were bypassed by the owner.
--   A policy that applies only TO oaos_company_definer keeps that scope for the
--   definer functions; no other role is affected.

CREATE OR REPLACE FUNCTION company.session_runs_as_owner() RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
  SELECT current_user = 'oaos_company_definer' OR EXISTS (
    SELECT 1 FROM pg_namespace n
     WHERE n.nspname = 'company'
       AND n.nspowner = (SELECT r.oid FROM pg_roles r WHERE r.rolname = current_user)
  );
$$;

CREATE POLICY console_setup_definer ON company.console_setup
  FOR ALL TO oaos_company_definer USING (true) WITH CHECK (true);
CREATE POLICY console_setup_codes_definer ON company.console_setup_codes
  FOR ALL TO oaos_company_definer USING (true) WITH CHECK (true);
CREATE POLICY console_login_codes_definer ON company.console_login_codes
  FOR ALL TO oaos_company_definer USING (true) WITH CHECK (true);
CREATE POLICY console_lockouts_definer ON company.console_lockouts
  FOR ALL TO oaos_company_definer USING (true) WITH CHECK (true);

-- Exactly what the 15 console functions and the triggers they fire touch.
GRANT USAGE ON SCHEMA company TO oaos_company_definer;
GRANT SELECT, INSERT ON company.organizations TO oaos_company_definer;
GRANT SELECT, INSERT ON company.members TO oaos_company_definer;
GRANT INSERT ON company.audit_events TO oaos_company_definer;
GRANT SELECT, INSERT, UPDATE ON company.console_setup TO oaos_company_definer;
GRANT SELECT, INSERT, UPDATE ON company.console_setup_codes TO oaos_company_definer;
GRANT SELECT, INSERT, UPDATE ON company.console_login_codes TO oaos_company_definer;
GRANT SELECT, INSERT, UPDATE ON company.console_sessions TO oaos_company_definer;
GRANT SELECT, INSERT, UPDATE, DELETE ON company.console_lockouts TO oaos_company_definer;
GRANT EXECUTE ON FUNCTION company.session_runs_as_owner() TO oaos_company_definer;
GRANT EXECUTE ON FUNCTION company.session_is_org_admin(uuid) TO oaos_company_definer;
GRANT EXECUTE ON FUNCTION company.member_bootstrap_allowed(uuid, uuid, text, text)
  TO oaos_company_definer;
GRANT EXECUTE ON FUNCTION company.console_session_is(uuid, uuid) TO oaos_company_definer;
