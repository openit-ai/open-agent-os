-- C04 real SQL responses; every fixture is discarded with this transaction.
BEGIN;
DO $verify$
DECLARE
  org_a uuid := gen_random_uuid();
  org_b uuid := gen_random_uuid();
  admin_a uuid := gen_random_uuid();
  owner_a uuid := gen_random_uuid();
  owner_b uuid := gen_random_uuid();
  owner_c uuid := gen_random_uuid();
  assistant_a uuid;
  assistant_b uuid;
  assistant_c uuid;
  replacement uuid;
  namespace_a uuid;
  namespace_b uuid;
  namespace_c uuid;
  replacement_namespace uuid;
  knowledge_a uuid;
  knowledge_b uuid;
  output_a uuid;
  output_b uuid;
  decision text;
  content text;
  rejected boolean;
BEGIN
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_a, 'c04-' || org_a::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (admin_a, org_a, 'c04-admin-' || admin_a::text, 'admin', 'active'),
    (owner_a, org_a, 'c04-owner-' || owner_a::text, 'member', 'active'),
    (owner_b, org_a, 'c04-owner-' || owner_b::text, 'member', 'active');

  PERFORM set_config('company.member_id', owner_a::text, true);
  SELECT r.decision, r.assistant_id, r.knowledge_namespace
    INTO decision, assistant_a, namespace_a FROM company.register_personal_assistant() r;
  IF decision IS DISTINCT FROM 'created' OR assistant_a IS NULL OR namespace_a IS NULL THEN
    RAISE EXCEPTION 'first assistant creation failed';
  END IF;
  SELECT r.decision, r.assistant_id INTO decision, replacement
    FROM company.register_personal_assistant() r;
  IF decision IS DISTINCT FROM 'existing' OR replacement IS DISTINCT FROM assistant_a THEN
    RAISE EXCEPTION 'registration was not idempotent';
  END IF;
  SELECT r.decision, r.item_id INTO decision, knowledge_a
    FROM company.put_assistant_knowledge(assistant_a, 'owner-a-knowledge') r;
  IF decision IS DISTINCT FROM 'created' OR knowledge_a IS NULL THEN
    RAISE EXCEPTION 'first knowledge write failed';
  END IF;
  SELECT r.decision, r.output_id INTO decision, output_a
    FROM company.record_assistant_output(assistant_a, 'owner-a-output') r;
  IF decision IS DISTINCT FROM 'created' OR output_a IS NULL THEN
    RAISE EXCEPTION 'first output write failed';
  END IF;
  SELECT r.decision, r.body INTO decision, content
    FROM company.get_assistant_knowledge(assistant_a, knowledge_a) r;
  IF decision IS DISTINCT FROM 'found' OR content IS DISTINCT FROM 'owner-a-knowledge' THEN
    RAISE EXCEPTION 'own knowledge response failed';
  END IF;
  SELECT r.decision, r.body INTO decision, content
    FROM company.get_assistant_output(assistant_a, output_a) r;
  IF decision IS DISTINCT FROM 'found' OR content IS DISTINCT FROM 'owner-a-output' THEN
    RAISE EXCEPTION 'own output response failed';
  END IF;

  PERFORM set_config('company.member_id', owner_b::text, true);
  SELECT r.decision, r.assistant_id, r.knowledge_namespace
    INTO decision, assistant_b, namespace_b FROM company.register_personal_assistant() r;
  IF decision IS DISTINCT FROM 'created' OR assistant_b IS NULL OR namespace_b IS NULL
     OR assistant_b = assistant_a OR namespace_b = namespace_a THEN
    RAISE EXCEPTION 'second assistant namespace was not distinct';
  END IF;
  SELECT r.decision, r.item_id INTO decision, knowledge_b
    FROM company.put_assistant_knowledge(assistant_b, 'owner-b-knowledge') r;
  IF decision IS DISTINCT FROM 'created' OR knowledge_b IS NULL THEN
    RAISE EXCEPTION 'second knowledge write failed';
  END IF;
  SELECT r.decision, r.output_id INTO decision, output_b
    FROM company.record_assistant_output(assistant_b, 'owner-b-output') r;
  IF decision IS DISTINCT FROM 'created' OR output_b IS NULL THEN
    RAISE EXCEPTION 'second output write failed';
  END IF;
  SELECT r.decision, r.body INTO decision, content
    FROM company.get_assistant_knowledge(assistant_b, knowledge_b) r;
  IF decision IS DISTINCT FROM 'found' OR content IS DISTINCT FROM 'owner-b-knowledge' THEN
    RAISE EXCEPTION 'second owner knowledge response failed';
  END IF;
  SELECT r.decision, r.body INTO decision, content
    FROM company.get_assistant_output(assistant_b, output_b) r;
  IF decision IS DISTINCT FROM 'found' OR content IS DISTINCT FROM 'owner-b-output' THEN
    RAISE EXCEPTION 'second owner output response failed';
  END IF;
  IF (SELECT count(*) FROM company.list_personal_assistants()) <> 1
     OR (SELECT l.assistant_id FROM company.list_personal_assistants() l) IS DISTINCT FROM assistant_b THEN
    RAISE EXCEPTION 'second owner assistant list leaked';
  END IF;
  IF company.assistant_execution_decision(assistant_a) IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'foreign assistant execution allowed';
  END IF;
  SELECT r.decision, r.body INTO decision, content
    FROM company.get_assistant_knowledge(assistant_a, knowledge_a) r;
  IF decision IS DISTINCT FROM 'denied' OR content IS NOT NULL THEN
    RAISE EXCEPTION 'foreign knowledge response leaked';
  END IF;
  SELECT r.decision, r.body INTO decision, content
    FROM company.get_assistant_output(assistant_a, output_a) r;
  IF decision IS DISTINCT FROM 'denied' OR content IS NOT NULL THEN
    RAISE EXCEPTION 'foreign output response leaked';
  END IF;
  IF (SELECT count(*) FROM company.assistant_knowledge) <> 1
     OR (SELECT count(*) FROM company.assistant_outputs) <> 1 THEN
    RAISE EXCEPTION 'foreign private rows visible under RLS';
  END IF;
  SELECT r.decision INTO decision FROM company.put_assistant_knowledge(assistant_a, 'bad') r;
  IF decision IS DISTINCT FROM 'denied' THEN RAISE EXCEPTION 'foreign knowledge write allowed'; END IF;
  SELECT r.decision INTO decision FROM company.record_assistant_output(assistant_a, 'bad') r;
  IF decision IS DISTINCT FROM 'denied' THEN RAISE EXCEPTION 'foreign output write allowed'; END IF;
  rejected := false;
  BEGIN
    INSERT INTO company.assistant_knowledge(org_id, assistant_id, owner_id, namespace, body)
      VALUES (org_a, assistant_a, owner_a, namespace_a, 'bad');
  EXCEPTION WHEN insufficient_privilege THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'direct foreign knowledge insert allowed'; END IF;

  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', owner_c::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_b, 'c04-' || org_b::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state)
    VALUES (owner_c, org_b, 'c04-owner-' || owner_c::text, 'admin', 'active');
  SELECT r.decision, r.assistant_id, r.knowledge_namespace
    INTO decision, assistant_c, namespace_c FROM company.register_personal_assistant() r;
  IF decision IS DISTINCT FROM 'created' OR assistant_c IS NULL OR namespace_c IN (namespace_a, namespace_b) THEN
    RAISE EXCEPTION 'second organization namespace failed';
  END IF;
  IF company.assistant_execution_decision(assistant_a) IS DISTINCT FROM 'denied'
     OR (SELECT count(*) FROM company.assistants) <> 1 THEN
    RAISE EXCEPTION 'cross-organization assistant visible';
  END IF;

  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', owner_a::text, true);
  IF (SELECT count(*) FROM company.list_personal_assistants()) <> 1
     OR (SELECT l.assistant_id FROM company.list_personal_assistants() l) IS DISTINCT FROM assistant_a THEN
    RAISE EXCEPTION 'first owner assistant list leaked';
  END IF;
  SELECT r.decision, r.body INTO decision, content
    FROM company.get_assistant_knowledge(assistant_b, knowledge_b) r;
  IF decision IS DISTINCT FROM 'denied' OR content IS NOT NULL THEN
    RAISE EXCEPTION 'first owner read second knowledge';
  END IF;
  SELECT r.decision, r.body INTO decision, content
    FROM company.get_assistant_output(assistant_b, output_b) r;
  IF decision IS DISTINCT FROM 'denied' OR content IS NOT NULL THEN
    RAISE EXCEPTION 'first owner read second output';
  END IF;
  IF company.revoke_personal_assistant(assistant_a) IS DISTINCT FROM 'revoked' THEN
    RAISE EXCEPTION 'assistant revocation failed';
  END IF;
  IF company.assistant_execution_decision(assistant_a) IS DISTINCT FROM 'denied'
     OR (SELECT count(*) FROM company.list_personal_assistants()) <> 0
     OR (SELECT count(*) FROM company.assistants WHERE state = 'active') <> 0
     OR (SELECT count(*) FROM company.assistant_knowledge) <> 0
     OR (SELECT count(*) FROM company.assistant_outputs) <> 0 THEN
    RAISE EXCEPTION 'revoked assistant remained accessible';
  END IF;
  SELECT r.decision, r.body INTO decision, content
    FROM company.get_assistant_knowledge(assistant_a, knowledge_a) r;
  IF decision IS DISTINCT FROM 'denied' OR content IS NOT NULL THEN
    RAISE EXCEPTION 'revoked knowledge remained accessible';
  END IF;
  SELECT r.decision, r.body INTO decision, content
    FROM company.get_assistant_output(assistant_a, output_a) r;
  IF decision IS DISTINCT FROM 'denied' OR content IS NOT NULL THEN
    RAISE EXCEPTION 'revoked output remained accessible';
  END IF;
  SELECT r.decision INTO decision FROM company.record_assistant_output(assistant_a, 'bad') r;
  IF decision IS DISTINCT FROM 'denied' THEN RAISE EXCEPTION 'revoked execution output allowed'; END IF;
  SELECT r.decision, r.assistant_id, r.knowledge_namespace
    INTO decision, replacement, replacement_namespace FROM company.register_personal_assistant() r;
  IF decision IS DISTINCT FROM 'created' OR replacement = assistant_a
     OR replacement_namespace = namespace_a THEN
    RAISE EXCEPTION 'replacement reused revoked assistant or namespace';
  END IF;
  SELECT r.decision, r.body INTO decision, content
    FROM company.get_assistant_knowledge(replacement, knowledge_a) r;
  IF decision IS DISTINCT FROM 'denied' OR content IS NOT NULL THEN
    RAISE EXCEPTION 'replacement read old namespace';
  END IF;
  PERFORM set_config('company.member_id', admin_a::text, true);
  UPDATE company.members SET state = 'suspended' WHERE id = owner_b;
  PERFORM set_config('company.member_id', owner_b::text, true);
  IF company.assistant_execution_decision(assistant_b) IS DISTINCT FROM 'denied'
     OR (SELECT count(*) FROM company.list_personal_assistants()) <> 0
     OR (SELECT count(*) FROM company.assistants) <> 0
     OR (SELECT count(*) FROM company.assistant_knowledge) <> 0
     OR (SELECT count(*) FROM company.assistant_outputs) <> 0 THEN
    RAISE EXCEPTION 'suspended owner remained active';
  END IF;
  SELECT r.decision, r.body INTO decision, content
    FROM company.get_assistant_output(assistant_b, output_b) r;
  IF decision IS DISTINCT FROM 'denied' OR content IS NOT NULL THEN
    RAISE EXCEPTION 'suspended owner read output';
  END IF;
  SELECT r.decision INTO decision FROM company.register_personal_assistant() r;
  IF decision IS DISTINCT FROM 'denied' THEN RAISE EXCEPTION 'suspended owner registered'; END IF;
  PERFORM set_config('company.member_id', admin_a::text, true);
  UPDATE company.members SET state = 'departed' WHERE id = owner_b;
  PERFORM set_config('company.member_id', owner_b::text, true);
  IF company.assistant_execution_decision(assistant_b) IS DISTINCT FROM 'denied'
     OR (SELECT count(*) FROM company.assistant_outputs) <> 0 THEN
    RAISE EXCEPTION 'departed owner remained active';
  END IF;
END;
$verify$;
ROLLBACK;
