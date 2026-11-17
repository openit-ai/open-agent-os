-- oaos-applied-with: 12
-- SPDX-License-Identifier: BUSL-1.1
-- Company edition: see LICENSE-COMPANY at the repository root.
--
-- Retention without root (company-design 10, O7). Applied by C17 and every
-- later phase through the maintenance path as the NOLOGIN Company owner role.
--
-- maintenance_purge (018) only ran as a superuser, so the daily retention unit
-- had to reach the postgres account through non-interactive sudo and could not
-- use NoNewPrivileges. It is now a SECURITY DEFINER function that
-- db-definers.sql hands to oaos_company_maintenance (NOLOGIN, BYPASSRLS, no
-- CREATE anywhere): the body is unchanged except that it also accepts that
-- role. Only the job login oaos_company_maint may EXECUTE it; that login has
-- no table privilege of its own, so the timer can purge but cannot read or
-- delete anything else. The maintenance role receives exactly the table
-- privileges the purge needs.

CREATE OR REPLACE FUNCTION company.maintenance_purge(p_audit_days integer DEFAULT 180,
  p_personal_days integer DEFAULT 30)
RETURNS TABLE(org_id uuid, audit_deleted bigint, documents_deleted bigint,
  knowledge_deleted bigint, outputs_deleted bigint)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = company, pg_catalog AS $$
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
  IF current_user IS DISTINCT FROM 'oaos_company_maintenance'
     AND NOT COALESCE((SELECT r.rolsuper FROM pg_roles r WHERE r.rolname = current_user), false) THEN
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

REVOKE ALL ON FUNCTION company.maintenance_purge(integer, integer)
  FROM PUBLIC, oaos_company_runtime;
GRANT EXECUTE ON FUNCTION company.maintenance_purge(integer, integer)
  TO oaos_company_maint;

GRANT USAGE ON SCHEMA company TO oaos_company_maintenance, oaos_company_maint;
GRANT SELECT ON company.organizations, company.members, company.member_revocations
  TO oaos_company_maintenance;
GRANT SELECT, DELETE ON company.documents, company.assistant_knowledge,
  company.assistant_outputs TO oaos_company_maintenance;
GRANT SELECT, INSERT, DELETE ON company.audit_events TO oaos_company_maintenance;
