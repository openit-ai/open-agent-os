-- oaos-applied-with: 11
-- SPDX-License-Identifier: BUSL-1.1
-- Company edition: see LICENSE-COMPANY at the repository root.
--
-- Console table write boundary (company-design 10, O9). Applied by C10 and every
-- later phase through the maintenance path as the NOLOGIN Company owner role.
--
-- Migration 013 granted the runtime group DML on every Company table, and the
-- 011 scope policies only check the organization. A runtime session with an
-- organization context could therefore
-- - DELETE (or UPDATE) its own console_lockouts row and lift the 5-failure lock,
-- - INSERT a console_setup_codes row with a hash it knows while G10 is pending
--   and confirm itself as the initial administrator,
-- - rewrite console_setup (state, requested account, initial administrator).
-- These three tables are only ever written by the SECURITY DEFINER console
-- functions of 011/020 (console_start_setup, console_confirm_admin,
-- console_note_failure, console_clear_failures, ...). The runtime keeps SELECT
-- (status and lock read-back under the 011 scope policies) and loses every
-- write; the definer functions keep working because they do not run as the
-- runtime role.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON company.console_lockouts
  FROM oaos_company_runtime, PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON company.console_setup_codes
  FROM oaos_company_runtime, PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON company.console_setup
  FROM oaos_company_runtime, PUBLIC;
GRANT SELECT ON company.console_lockouts, company.console_setup_codes,
  company.console_setup TO oaos_company_runtime;
