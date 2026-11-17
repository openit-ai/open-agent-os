-- SPDX-License-Identifier: BUSL-1.1
-- Company edition: see LICENSE-COMPANY at the repository root.
-- SECURITY DEFINER ownership (company-design 10, O6/O7). Run by migrate.sh
-- through the local postgres maintenance account after the migrations of a run
-- (and by --check when it finds a definer function still owned by the owner
-- role). Idempotent, not checksummed.
--
-- A function owner needs CREATE on the function's schema, so only the
-- maintenance account can hand a function to a role without DDL rights:
-- - console_* definer functions (011/020) -> oaos_company_definer, once
--   migration 022 created that role's policies and grants;
-- - vault_* and maintenance_* definer functions (023/024) ->
--   oaos_company_maintenance (BYPASSRLS, NOLOGIN, explicit grants only).
-- The read-back (company_db_runtime_boundary) fails while any SECURITY DEFINER
-- function in the schema belongs to a role with CREATE on the schema or the
-- database, a superuser or a login role.
DO $definers$
DECLARE
  f record;
  target text;
BEGIN
  IF to_regnamespace('company') IS NULL THEN
    RETURN;
  END IF;
  FOR f IN
    SELECT p.oid::regprocedure AS sig, p.proname, p.proowner::regrole::text AS owner
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'company' AND p.prosecdef
  LOOP
    target := CASE
      WHEN f.proname LIKE 'console\_%' THEN 'oaos_company_definer'
      WHEN f.proname LIKE 'vault\_%' OR f.proname LIKE 'maintenance\_%'
        THEN 'oaos_company_maintenance'
    END;
    IF target IS NULL THEN
      RAISE EXCEPTION 'SECURITY DEFINER function % has no definer role', f.sig;
    END IF;
    IF target = 'oaos_company_definer' AND (
         to_regclass('company.schema_migrations') IS NULL
         OR NOT EXISTS (SELECT 1 FROM company.schema_migrations WHERE version = 22)) THEN
      CONTINUE;
    END IF;
    IF to_regrole(target) IS NULL THEN
      RAISE EXCEPTION 'definer role % is missing', target;
    END IF;
    IF f.owner IS DISTINCT FROM target THEN
      EXECUTE format('ALTER FUNCTION %s OWNER TO %I', f.sig, target);
    END IF;
  END LOOP;
END;
$definers$;
