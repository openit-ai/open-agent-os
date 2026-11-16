-- oaos-applied-with: 2
-- Runtime privilege boundary (audit P1-2). Applied by C03 and every later phase
-- through the maintenance path as the NOLOGIN Company owner role after
-- db-roles.sql adopted the schema. The libpq runtime role reaches Company data
-- only through the oaos_company_runtime group granted here:
-- - DML on application tables, still bound by the existing FORCE RLS policies;
-- - audit_events: SELECT and INSERT only (append-only for the application);
-- - schema_migrations: SELECT only;
-- - no TRUNCATE, REFERENCES or TRIGGER anywhere, no CREATE on the schema, and
--   no ownership, so the application cannot disable RLS or replace functions.
DO $owner$
DECLARE me oid := (SELECT oid FROM pg_roles WHERE rolname = current_user);
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'company' AND c.relowner <> me
  ) OR (SELECT nspowner FROM pg_namespace WHERE nspname = 'company') <> me THEN
    RAISE EXCEPTION 'Company objects must be owned by the migration owner role';
  END IF;
  IF pg_has_role('oaos_company_runtime', current_user, 'MEMBER') THEN
    RAISE EXCEPTION 'runtime group must not be a member of the owner role';
  END IF;
END;
$owner$;

REVOKE ALL ON SCHEMA company FROM PUBLIC;
GRANT USAGE ON SCHEMA company TO oaos_company_runtime;
REVOKE ALL ON ALL TABLES IN SCHEMA company FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA company TO oaos_company_runtime;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON company.schema_migrations FROM oaos_company_runtime;
REVOKE UPDATE, DELETE, TRUNCATE ON company.audit_events FROM oaos_company_runtime, PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA company TO oaos_company_runtime;

-- Objects created by later base migrations (owned by this same role) receive
-- the same application grant automatically. Append-only tables added later
-- must revoke UPDATE/DELETE explicitly, as migration 018 does for retention.
ALTER DEFAULT PRIVILEGES IN SCHEMA company
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO oaos_company_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA company
  GRANT EXECUTE ON FUNCTIONS TO oaos_company_runtime;
