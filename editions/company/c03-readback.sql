-- Real C03 responses against C01 tables and C03 functions; no fixture persists.
BEGIN;
DO $verify$
DECLARE
  org_a uuid := gen_random_uuid();
  org_b uuid := gen_random_uuid();
  admin_a uuid := gen_random_uuid();
  admin_b uuid := gen_random_uuid();
  owner_a uuid := gen_random_uuid();
  owner_b uuid := gen_random_uuid();
  response text;
  resolved uuid;
  resolved_role text;
  account uuid;
  rejected boolean;
BEGIN
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_a, 'c03-' || org_a::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (admin_a, org_a, 'c03-admin-' || admin_a::text, 'admin', 'active'),
    (owner_a, org_a, 'c03-owner-' || owner_a::text, 'member', 'active'),
    (owner_b, org_a, 'c03-owner-' || owner_b::text, 'member', 'active');

  SELECT decision, mapped_member_id, mapped_role INTO response, resolved, resolved_role
    FROM company.resolve_project_member('c03-admin-' || admin_a::text);
  IF response IS DISTINCT FROM 'allow' OR resolved IS DISTINCT FROM admin_a
     OR resolved_role IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION 'admin mapping lookup failed';
  END IF;
  SELECT decision, mapped_member_id INTO response, resolved
    FROM company.resolve_project_member('c03-owner-' || owner_a::text);
  IF response IS DISTINCT FROM 'allow' OR resolved IS DISTINCT FROM owner_a THEN
    RAISE EXCEPTION 'member mapping lookup failed';
  END IF;
  SELECT decision INTO response FROM company.resolve_project_member('c03-unknown');
  IF response IS DISTINCT FROM 'unregistered' THEN
    RAISE EXCEPTION 'unregistered Project account was accepted';
  END IF;
  rejected := false;
  BEGIN
    INSERT INTO company.members(org_id, project_user_id, role, state)
      VALUES (org_a, 'c03-owner-' || owner_a::text, 'member', 'active');
  EXCEPTION WHEN unique_violation THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'duplicate Project mapping was accepted'; END IF;

  SELECT decision, account_id INTO response, account
    FROM company.bind_external_account(owner_a, 'fixture', 'workspace', 'subject');
  IF response IS DISTINCT FROM 'created' OR account IS NULL THEN
    RAISE EXCEPTION 'external mapping creation failed';
  END IF;
  SELECT decision, mapped_member_id INTO response, resolved
    FROM company.resolve_external_member('fixture', 'workspace', 'subject');
  IF response IS DISTINCT FROM 'allow' OR resolved IS DISTINCT FROM owner_a THEN
    RAISE EXCEPTION 'external mapping lookup failed';
  END IF;
  SELECT decision INTO response
    FROM company.bind_external_account(owner_b, 'fixture', 'workspace', 'subject');
  IF response IS DISTINCT FROM 'duplicate' THEN
    RAISE EXCEPTION 'duplicate external subject was accepted';
  END IF;
  SELECT decision INTO response
    FROM company.resolve_external_member('fixture', 'workspace', 'unknown');
  IF response IS DISTINCT FROM 'unregistered' THEN
    RAISE EXCEPTION 'unregistered external subject was accepted';
  END IF;
  SELECT decision INTO response
    FROM company.bind_external_account(gen_random_uuid(), 'fixture', 'workspace', 'unknown');
  IF response IS DISTINCT FROM 'unregistered' THEN
    RAISE EXCEPTION 'unregistered member was bound';
  END IF;
  SELECT decision INTO response
    FROM company.bind_external_account(owner_b, 'fixture', 'workspace', 'suspended');
  IF response IS DISTINCT FROM 'created' THEN
    RAISE EXCEPTION 'second external mapping creation failed';
  END IF;

  UPDATE company.members SET state = 'suspended' WHERE id = owner_b;
  SELECT decision INTO response
    FROM company.resolve_project_member('c03-owner-' || owner_b::text);
  IF response IS DISTINCT FROM 'inactive' THEN
    RAISE EXCEPTION 'suspended Project account was accepted';
  END IF;
  SELECT decision INTO response
    FROM company.resolve_external_member('fixture', 'workspace', 'suspended');
  IF response IS DISTINCT FROM 'inactive' THEN
    RAISE EXCEPTION 'suspended external account was accepted';
  END IF;
  SELECT decision INTO response
    FROM company.bind_external_account(owner_b, 'fixture', 'workspace', 'suspended');
  IF response IS DISTINCT FROM 'inactive' THEN
    RAISE EXCEPTION 'suspended member was bound';
  END IF;

  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', admin_b::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_b, 'c03-' || org_b::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state)
    VALUES (admin_b, org_b, 'c03-admin-' || admin_b::text, 'admin', 'active');
  SELECT decision INTO response
    FROM company.resolve_project_member('c03-owner-' || owner_a::text);
  IF response IS DISTINCT FROM 'unregistered' THEN
    RAISE EXCEPTION 'cross-organization Project mapping visible';
  END IF;
  SELECT decision INTO response
    FROM company.resolve_external_member('fixture', 'workspace', 'subject');
  IF response IS DISTINCT FROM 'unregistered' THEN
    RAISE EXCEPTION 'cross-organization external mapping visible';
  END IF;

  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  UPDATE company.members SET state = 'departed' WHERE id = owner_a;
  SELECT decision INTO response
    FROM company.resolve_project_member('c03-owner-' || owner_a::text);
  IF response IS DISTINCT FROM 'departed' THEN
    RAISE EXCEPTION 'departed Project account was accepted';
  END IF;
  SELECT decision INTO response
    FROM company.resolve_external_member('fixture', 'workspace', 'subject');
  IF response IS DISTINCT FROM 'departed' THEN
    RAISE EXCEPTION 'departed external account was accepted';
  END IF;
  IF (SELECT state FROM company.external_accounts WHERE id = account) IS DISTINCT FROM 'revoked' THEN
    RAISE EXCEPTION 'departure did not revoke external mapping';
  END IF;
  PERFORM set_config('company.member_id', owner_b::text, true);
  SELECT decision INTO response
    FROM company.resolve_project_member('c03-admin-' || admin_a::text);
  IF response IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'inactive non-admin could inspect mappings';
  END IF;
END;
$verify$;
ROLLBACK;
