-- SPDX-License-Identifier: BUSL-1.1
-- Company edition: see LICENSE-COMPANY at the repository root.
-- Company owner/runtime role separation. Run by migrate.sh through the local
-- postgres maintenance account (never by the runtime role) before a migration is
-- applied, with psql variables :owner (NOLOGIN schema owner) and :runtime (the
-- libpq login used by the installer, console and service). Idempotent.
--
-- - :owner owns the company schema and every object in it. It cannot log in;
--   migrations reach it only through the maintenance path with SET ROLE.
-- - oaos_company_runtime is a NOLOGIN privilege group. Migration 013 grants it
--   the minimum DML (audit rows: SELECT/INSERT only) and :runtime is its member.
-- - :runtime must not be a superuser, BYPASSRLS, or a member of :owner, so the
--   application can never TRUNCATE audit rows, disable RLS or replace functions.
-- - oaos_company_definer (O6) owns the SECURITY DEFINER console functions. It is
--   NOLOGIN, has no CREATE on the schema or the database and is not a member of
--   :owner, so a definer function can only use the table privileges migration
--   022 grants it. :owner is a member of it (not the reverse) so a later
--   amendment can still replace those functions. db-definers.sql moves the
--   function ownership after the migrations of a run.
SELECT set_config('oaos.owner', :'owner', true);
SELECT set_config('oaos.runtime', :'runtime', true);

SELECT format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE', :'owner')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner') \gexec
SELECT 'CREATE ROLE oaos_company_runtime NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oaos_company_runtime') \gexec
SELECT 'CREATE ROLE oaos_company_definer NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oaos_company_definer') \gexec

DO $roles$
DECLARE
  owner_name text := current_setting('oaos.owner');
  runtime_name text := current_setting('oaos.runtime');
  owner_role pg_roles%ROWTYPE;
  runtime_role pg_roles%ROWTYPE;
BEGIN
  SELECT * INTO owner_role FROM pg_roles WHERE rolname = owner_name;
  SELECT * INTO runtime_role FROM pg_roles WHERE rolname = runtime_name;
  IF runtime_role.oid IS NULL THEN
    RAISE EXCEPTION 'runtime role does not exist';
  END IF;
  IF owner_role.rolcanlogin OR owner_role.rolsuper OR owner_role.rolbypassrls THEN
    RAISE EXCEPTION 'Company owner role must be NOLOGIN, NOSUPERUSER and NOBYPASSRLS';
  END IF;
  IF runtime_role.rolsuper OR runtime_role.rolbypassrls THEN
    RAISE EXCEPTION 'runtime role must not bypass row level security';
  END IF;
  IF pg_has_role(runtime_name, owner_name, 'MEMBER') THEN
    RAISE EXCEPTION 'runtime role must not be a member of the Company owner role';
  END IF;
  -- The definer role never gains login, RLS bypass, DDL or the owner's rights.
  ALTER ROLE oaos_company_definer NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
    NOCREATEROLE NOREPLICATION;
  IF pg_has_role('oaos_company_definer', owner_name, 'MEMBER')
     OR pg_has_role('oaos_company_definer', 'oaos_company_runtime', 'MEMBER')
     OR pg_has_role(runtime_name, 'oaos_company_definer', 'MEMBER') THEN
    RAISE EXCEPTION 'definer role must stay separate from the owner and runtime roles';
  END IF;
  EXECUTE format('REVOKE CREATE, TEMPORARY ON DATABASE %I FROM oaos_company_definer',
    current_database());
  IF to_regnamespace('company') IS NOT NULL THEN
    REVOKE CREATE ON SCHEMA company FROM oaos_company_definer;
  END IF;
END;
$roles$;

SELECT format('GRANT oaos_company_definer TO %I', :'owner')
 WHERE NOT pg_has_role(:'owner', 'oaos_company_definer', 'MEMBER') \gexec

SELECT format('GRANT oaos_company_runtime TO %I', :'runtime')
 WHERE NOT pg_has_role(:'runtime', 'oaos_company_runtime', 'MEMBER') \gexec
SELECT format('GRANT CONNECT, CREATE ON DATABASE %I TO %I', current_database(), :'owner') \gexec

-- Adopt objects created before the separation (C01 runs as the libpq role, and
-- an E installed before this change has the runtime role as owner).
DO $adopt$
DECLARE
  owner_name text := current_setting('oaos.owner');
  obj record;
BEGIN
  IF to_regnamespace('company') IS NULL THEN
    RETURN;
  END IF;
  IF (SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'company')
     IS DISTINCT FROM quote_ident(owner_name) THEN
    EXECUTE format('ALTER SCHEMA company OWNER TO %I', owner_name);
  END IF;
  FOR obj IN
    SELECT c.oid::regclass AS name, c.relkind
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'company' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
       AND c.relowner <> to_regrole(owner_name)
  LOOP
    EXECUTE format('ALTER %s %s OWNER TO %I',
      CASE obj.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
        WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END, obj.name, owner_name);
  END LOOP;
  -- Sequences owned by a column follow their table; standalone ones move here.
  FOR obj IN
    SELECT c.oid::regclass AS name
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'company' AND c.relkind = 'S'
       AND c.relowner <> to_regrole(owner_name)
  LOOP
    EXECUTE format('ALTER SEQUENCE %s OWNER TO %I', obj.name, owner_name);
  END LOOP;
  -- Functions handed to a definer role (db-definers.sql) keep that owner.
  FOR obj IN
    SELECT p.oid::regprocedure AS name, p.prokind
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'company' AND p.proowner <> to_regrole(owner_name)
       AND p.proowner::regrole::text NOT IN ('oaos_company_definer', 'oaos_company_maintenance')
  LOOP
    EXECUTE format('ALTER %s %s OWNER TO %I',
      CASE obj.prokind WHEN 'p' THEN 'PROCEDURE' WHEN 'a' THEN 'AGGREGATE'
        ELSE 'FUNCTION' END, obj.name, owner_name);
  END LOOP;
  FOR obj IN
    SELECT t.oid::regtype AS name
      FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
     WHERE n.nspname = 'company' AND t.typtype IN ('c', 'd', 'e', 'r')
       AND (t.typrelid = 0 OR (SELECT relkind FROM pg_class WHERE oid = t.typrelid) = 'c')
       AND t.typowner <> to_regrole(owner_name)
  LOOP
    EXECUTE format('ALTER TYPE %s OWNER TO %I', obj.name, owner_name);
  END LOOP;
  -- The runtime role must still read migration state while a run is between
  -- adoption and migration 013 (which grants the full application privileges).
  GRANT USAGE ON SCHEMA company TO oaos_company_runtime;
  IF to_regclass('company.schema_migrations') IS NOT NULL THEN
    GRANT SELECT ON company.schema_migrations TO oaos_company_runtime;
  END IF;
END;
$adopt$;
