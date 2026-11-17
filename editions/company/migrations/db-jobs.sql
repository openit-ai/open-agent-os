-- SPDX-License-Identifier: BUSL-1.1
-- Company edition: see LICENSE-COMPANY at the repository root.
-- Database roles of the scheduled Company jobs (company-design 10, O7). Run
-- through the local postgres maintenance account by company_db_jobs_ensure
-- (C02 and every C03+ phase) and included by db-roles.sql. Idempotent, not
-- checksummed, no psql variables. Passwords are never set here: the Company
-- account keeps them in its own 0600 libpq password file and
-- company_db_jobs_ensure sends only SCRAM verifiers through standard input.
--
-- - oaos_company_backup (LOGIN, BYPASSRLS): the daily pg_dump. FORCE RLS makes
--   a dump fail for any role that does not bypass RLS, so this role bypasses
--   it, but it is read-only: pg_read_all_data (PostgreSQL 14+) or explicit
--   SELECT on the Company schema, no write privilege, no membership in the
--   owner, runtime or definer roles, no superuser, DDL or role creation.
-- - oaos_company_maintenance (NOLOGIN, BYPASSRLS): owns the SECURITY DEFINER
--   maintenance_* and vault_* functions (db-definers.sql) with exactly the table
--   privileges their migrations grant; no CREATE on the schema or database.
-- - oaos_company_maint (LOGIN): the retention timer and the vault rotation
--   procedure. No table privilege at all and no role membership; it can only
--   EXECUTE the maintenance/vault functions granted by migrations 023/024.
SELECT 'CREATE ROLE oaos_company_backup LOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oaos_company_backup') \gexec
SELECT 'CREATE ROLE oaos_company_maintenance NOLOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oaos_company_maintenance') \gexec
SELECT 'CREATE ROLE oaos_company_maint LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oaos_company_maint') \gexec

DO $jobs$
DECLARE
  member_of record;
BEGIN
  -- Attributes are re-asserted on every run.
  ALTER ROLE oaos_company_backup LOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE
    NOREPLICATION INHERIT;
  ALTER ROLE oaos_company_maintenance NOLOGIN NOSUPERUSER BYPASSRLS NOCREATEDB
    NOCREATEROLE NOREPLICATION;
  ALTER ROLE oaos_company_maint LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
    NOREPLICATION NOINHERIT;
  -- No job role may hold another role, except the backup role reading data.
  FOR member_of IN
    SELECT m.roleid::regrole::text AS granted, m.member::regrole::text AS grantee
      FROM pg_auth_members m
     WHERE m.member::regrole::text IN ('oaos_company_backup', 'oaos_company_maintenance',
                                       'oaos_company_maint')
       AND NOT (m.member::regrole::text = 'oaos_company_backup'
                AND m.roleid::regrole::text = 'pg_read_all_data')
  LOOP
    EXECUTE format('REVOKE %I FROM %I', member_of.granted, member_of.grantee);
  END LOOP;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO oaos_company_backup, oaos_company_maint',
    current_database());
  EXECUTE format('REVOKE CREATE ON DATABASE %I FROM oaos_company_backup, '
    'oaos_company_maintenance, oaos_company_maint', current_database());
  IF to_regrole('pg_read_all_data') IS NOT NULL THEN
    GRANT pg_read_all_data TO oaos_company_backup;
  END IF;
  IF to_regnamespace('company') IS NOT NULL THEN
    REVOKE CREATE ON SCHEMA company
      FROM oaos_company_backup, oaos_company_maintenance, oaos_company_maint;
    GRANT USAGE ON SCHEMA company
      TO oaos_company_backup, oaos_company_maintenance, oaos_company_maint;
    -- Explicit read for servers without pg_read_all_data; never any write.
    GRANT SELECT ON ALL TABLES IN SCHEMA company TO oaos_company_backup;
    GRANT SELECT ON ALL SEQUENCES IN SCHEMA company TO oaos_company_backup;
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
      ON ALL TABLES IN SCHEMA company FROM oaos_company_backup, oaos_company_maint;
    REVOKE ALL ON ALL TABLES IN SCHEMA company FROM oaos_company_maint;
  END IF;
END;
$jobs$;
