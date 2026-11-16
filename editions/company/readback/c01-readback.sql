-- SPDX-License-Identifier: BUSL-1.1
-- Company edition: see LICENSE-COMPANY at the repository root.
-- C01 read-back against the 001 schema (and the 019 member write boundary once a
-- later phase applied it). Every fixture lives in one transaction that always
-- rolls back. Each passed section emits "oaos-check: <name>" for c01-verify.sh.
BEGIN;
DO $verify$
DECLARE
  org_a uuid := gen_random_uuid();
  org_b uuid := gen_random_uuid();
  admin_a uuid := gen_random_uuid();
  owner_a uuid := gen_random_uuid();
  owner_b uuid := gen_random_uuid();
  assistant_a uuid := gen_random_uuid();
  secret_a uuid := gen_random_uuid();
  document_a uuid := gen_random_uuid();
  owner_c uuid := gen_random_uuid();
  changed integer;
  rejected boolean;
BEGIN
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_a, 'c01-fixture-a-' || org_a::text);
  -- The session administrator bootstraps its organization first (019).
  INSERT INTO company.members(id, org_id, project_user_id, role, state)
    VALUES (admin_a, org_a, 'c01-admin-' || admin_a::text, 'admin', 'active');
  INSERT INTO company.members(id, org_id, project_user_id, role, state)
    VALUES (owner_a, org_a, 'c01-owner-' || owner_a::text, 'member', 'active');
  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', owner_b::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_b, 'c01-fixture-b-' || org_b::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state)
    VALUES (owner_b, org_b, 'c01-owner-' || owner_b::text, 'admin', 'active');
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', owner_a::text, true);
  INSERT INTO company.assistants(id, org_id, owner_id) VALUES (assistant_a, org_a, owner_a);
  INSERT INTO company.secrets(id, org_id, owner_id, provider, purpose, ciphertext, nonce, key_version)
    VALUES (secret_a, org_a, owner_a, 'fixture', 'test', decode('01','hex'), decode('02','hex'), 1);
  INSERT INTO company.documents(id, org_id, source, source_id, owner_id, source_acl_version)
    VALUES (document_a, org_a, 'personal', 'c01-' || document_a::text, owner_a, '1');
  INSERT INTO company.chunks(org_id, document_id, ordinal, body) VALUES (org_a, document_a, 0, 'fixture');

  IF (SELECT count(*) FROM company.organizations) <> 1 OR
     (SELECT count(*) FROM company.assistants) <> 1 OR
     (SELECT count(*) FROM company.secrets) <> 1 OR
     (SELECT count(*) FROM company.documents) <> 1 OR
     (SELECT count(*) FROM company.chunks) <> 1 THEN
    RAISE EXCEPTION 'owner baseline is not visible';
  END IF;
  PERFORM set_config('company.member_id', admin_a::text, true);
  IF (SELECT count(*) FROM company.assistants) <> 0 OR
     (SELECT count(*) FROM company.secrets WHERE owner_id IS NOT NULL) <> 0 OR
     (SELECT count(*) FROM company.documents) <> 0 OR
     (SELECT count(*) FROM company.chunks) <> 0 THEN
    RAISE EXCEPTION 'cross-owner row was visible';
  END IF;
  RAISE NOTICE 'oaos-check: owner_isolation';
  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', owner_b::text, true);
  IF (SELECT count(*) FROM company.organizations) <> 1 OR
     (SELECT count(*) FROM company.members) <> 1 OR
     (SELECT count(*) FROM company.assistants) <> 0 OR
     (SELECT count(*) FROM company.documents) <> 0 THEN
    RAISE EXCEPTION 'cross-organization row was visible';
  END IF;
  RAISE NOTICE 'oaos-check: organization_isolation';

  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', owner_b::text, true);
  rejected := false;
  BEGIN
    INSERT INTO company.external_accounts(org_id, member_id, provider, workspace_id, subject_id)
      VALUES (org_a, owner_b, 'fixture', 'workspace', 'subject');
  EXCEPTION WHEN foreign_key_violation THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'cross-org FK accepted'; END IF;

  PERFORM set_config('company.member_id', owner_a::text, true);
  rejected := false;
  BEGIN
    INSERT INTO company.assistants(org_id, owner_id) VALUES (org_a, owner_a);
  EXCEPTION WHEN unique_violation THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'assistant partial UNIQUE accepted duplicate'; END IF;
  PERFORM set_config('company.member_id', admin_a::text, true);
  rejected := false;
  BEGIN
    INSERT INTO company.members(org_id, project_user_id, role, state)
      VALUES (org_a, 'bad-role', 'invalid', 'active');
  EXCEPTION WHEN check_violation THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'member CHECK accepted invalid role'; END IF;
  PERFORM set_config('company.member_id', owner_a::text, true);

  rejected := false;
  BEGIN
    UPDATE company.assistants SET owner_id = admin_a WHERE id = assistant_a;
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'assistant owner changed'; END IF;
  rejected := false;
  BEGIN
    UPDATE company.secrets SET owner_id = admin_a WHERE id = secret_a;
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'secret owner changed'; END IF;
  rejected := false;
  BEGIN
    UPDATE company.documents SET owner_id = admin_a WHERE id = document_a;
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'document owner changed'; END IF;
  RAISE NOTICE 'oaos-check: constraint_rejection';

  rejected := false;
  PERFORM set_config('company.member_id', admin_a::text, true);
  BEGIN
    UPDATE company.members SET state = 'suspended' WHERE id = admin_a;
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'last active administrator suspended'; END IF;

  PERFORM set_config('company.member_id', owner_a::text, true);
  INSERT INTO company.external_accounts(org_id, member_id, provider, workspace_id, subject_id, state)
    VALUES (org_a, owner_a, 'fixture', 'workspace', 'subject', 'active');
  PERFORM set_config('company.member_id', admin_a::text, true);
  rejected := false;
  BEGIN
    UPDATE company.external_accounts SET member_id = admin_a WHERE member_id = owner_a;
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'external account owner changed'; END IF;
  UPDATE company.members SET state = 'departed' WHERE id = owner_a;
  IF (SELECT count(*) FROM company.external_accounts WHERE member_id = owner_a AND state = 'revoked') <> 1 THEN
    RAISE EXCEPTION 'departed member external account stayed active';
  END IF;
  rejected := false;
  BEGIN
    UPDATE company.members SET state = 'active' WHERE id = owner_a;
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'departed member reactivated'; END IF;
  RAISE NOTICE 'oaos-check: member_lifecycle';

  -- Member write boundary (019, design O1) once a later phase applied it.
  IF to_regprocedure('company.session_is_org_admin(uuid)') IS NOT NULL THEN
    PERFORM set_config('company.member_id', admin_a::text, true);
    INSERT INTO company.members(id, org_id, project_user_id, role, state)
      VALUES (owner_c, org_a, 'c01-owner-' || owner_c::text, 'member', 'active');
    PERFORM set_config('company.member_id', owner_c::text, true);
    UPDATE company.members SET role = 'admin' WHERE id = owner_c;
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 0 OR (SELECT role FROM company.members WHERE id = owner_c) <> 'member' THEN
      RAISE EXCEPTION 'member promoted itself to administrator';
    END IF;
    UPDATE company.members SET state = 'suspended' WHERE id = admin_a;
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 0 THEN RAISE EXCEPTION 'member suspended an administrator'; END IF;
    UPDATE company.members SET project_user_id = 'c01-hijack' WHERE id = admin_a;
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 0 THEN RAISE EXCEPTION 'member rewrote another member mapping'; END IF;
    DELETE FROM company.members WHERE id = owner_a;
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 0 THEN RAISE EXCEPTION 'member deleted another member'; END IF;
    rejected := false;
    BEGIN
      INSERT INTO company.members(org_id, project_user_id, role, state)
        VALUES (org_a, 'c01-rogue-' || owner_c::text, 'admin', 'active');
    EXCEPTION WHEN insufficient_privilege THEN rejected := true;
    END;
    IF NOT rejected THEN RAISE EXCEPTION 'member created an administrator'; END IF;
    -- Cross-organization: org B administrator with org A or org B scope.
    PERFORM set_config('company.member_id', owner_b::text, true);
    UPDATE company.members SET role = 'admin' WHERE id = owner_c;
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 0 THEN RAISE EXCEPTION 'foreign administrator changed a member'; END IF;
    PERFORM set_config('company.org_id', org_b::text, true);
    rejected := false;
    BEGIN
      INSERT INTO company.members(org_id, project_user_id, role, state)
        VALUES (org_a, 'c01-cross-' || owner_b::text, 'admin', 'active');
    EXCEPTION WHEN insufficient_privilege THEN rejected := true;
    END;
    IF NOT rejected THEN RAISE EXCEPTION 'cross-organization member insert accepted'; END IF;
    UPDATE company.members SET role = 'admin' WHERE id = owner_c;
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 0 THEN RAISE EXCEPTION 'cross-organization member update accepted'; END IF;
    -- Administrators change others, never themselves.
    PERFORM set_config('company.org_id', org_a::text, true);
    PERFORM set_config('company.member_id', admin_a::text, true);
    UPDATE company.members SET role = 'admin' WHERE id = owner_c;
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 1 THEN RAISE EXCEPTION 'administrator could not promote a member'; END IF;
    rejected := false;
    BEGIN
      UPDATE company.members SET role = 'member' WHERE id = admin_a;
    EXCEPTION WHEN raise_exception THEN
      rejected := SQLERRM LIKE '%own role or state%';
    END;
    IF NOT rejected THEN RAISE EXCEPTION 'administrator changed its own role'; END IF;
    RAISE NOTICE 'oaos-check: member_write_boundary';
  END IF;
END;
$verify$;
ROLLBACK;
\echo oaos-check: rolled_back
