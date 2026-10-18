-- C06 uses synthetic fixtures and always rolls back. No credential value is printed.
BEGIN;
DO $verify$
DECLARE
  org_a uuid := gen_random_uuid();
  org_b uuid := gen_random_uuid();
  owner_a uuid := gen_random_uuid();
  owner_b uuid := gen_random_uuid();
  owner_c uuid := gen_random_uuid();
  assistant_a uuid;
  assistant_b uuid;
  assistant_c uuid;
  namespace_b uuid;
  secret_a uuid;
  secret_a2 uuid;
  secret_b uuid;
  secret_c uuid;
  output_a uuid;
  output_a2 uuid;
  decision text;
  revoke_decision text;
  body text;
  rejected boolean;
  old_envelope bytea := decode(repeat('ab', 40), 'hex');
  next_envelope bytea := decode(repeat('cd', 40), 'hex');
  old_nonce bytea := decode(repeat('01', 12), 'hex');
BEGIN
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', owner_a::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_a, 'c06-' || org_a::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (owner_a, org_a, 'c06-' || owner_a::text, 'admin', 'active'),
    (owner_b, org_a, 'c06-' || owner_b::text, 'member', 'active');
  SELECT r.assistant_id INTO assistant_a FROM company.register_personal_assistant() r;
  SELECT r.secret_id INTO secret_a
    FROM company.store_personal_secret('fixture', 'personal', old_envelope, old_nonce, 1) r;
  IF assistant_a IS NULL OR secret_a IS NULL THEN
    RAISE EXCEPTION 'owner fixture is incomplete';
  END IF;

  SELECT r.decision, r.output_id INTO decision, output_a
    FROM company.record_isolated_output(assistant_a, secret_a, 'fixture', 'answer-a') r;
  IF decision IS DISTINCT FROM 'created' OR output_a IS NULL THEN
    RAISE EXCEPTION 'owner bound output was not recorded';
  END IF;
  IF company.identity_isolation_decision(assistant_a, secret_a, 'fixture', output_a)
     IS DISTINCT FROM 'allow' THEN
    RAISE EXCEPTION 'owner identity binding was denied';
  END IF;
  SELECT r.decision, r.body INTO decision, body
    FROM company.get_isolated_output(assistant_a, secret_a, 'fixture', output_a) r;
  IF decision IS DISTINCT FROM 'found' OR body IS DISTINCT FROM 'answer-a' THEN
    RAISE EXCEPTION 'owner bound output was not readable';
  END IF;
  IF (SELECT count(*) FROM company.assistant_outputs o
      WHERE o.id = output_a AND o.secret_id = secret_a AND o.provider = 'fixture') <> 1 THEN
    RAISE EXCEPTION 'owner output was not bound to the credential and provider';
  END IF;
  IF (SELECT count(*) FROM company.audit_events
      WHERE event_type = 'identity_binding' AND action = 'output_bound'
        AND resource_ref = output_a::text
        AND metadata->>'provider' = 'fixture'
        AND metadata->>'secret_id' = secret_a::text
        AND metadata ? 'ciphertext' = false) <> 1 THEN
    RAISE EXCEPTION 'binding audit event missing or leaked credential material';
  END IF;

  -- A different provider never matches the stored credential.
  IF company.identity_isolation_decision(assistant_a, secret_a, 'other', output_a)
     IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'provider mismatch was allowed';
  END IF;
  SELECT r.decision INTO decision
    FROM company.get_isolated_output(assistant_a, secret_a, 'other', output_a) r;
  IF decision IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'provider mismatch read was allowed';
  END IF;
  SELECT r.decision INTO decision
    FROM company.record_isolated_output(assistant_a, secret_a, 'other', 'answer-a') r;
  IF decision IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'provider mismatch recording was allowed';
  END IF;

  -- Another owner cannot reach the assistant, credential or output.
  PERFORM set_config('company.member_id', owner_b::text, true);
  SELECT r.assistant_id INTO assistant_b FROM company.register_personal_assistant() r;
  SELECT a.knowledge_namespace INTO namespace_b FROM company.assistants a
    WHERE a.id = assistant_b;
  SELECT r.secret_id INTO secret_b
    FROM company.store_personal_secret('fixture', 'personal', old_envelope, old_nonce, 1) r;
  IF assistant_b IS NULL OR secret_b IS NULL THEN
    RAISE EXCEPTION 'second owner fixture is incomplete';
  END IF;
  IF company.identity_isolation_decision(assistant_a, secret_a, 'fixture', output_a)
     IS DISTINCT FROM 'denied'
     OR (SELECT count(*) FROM company.assistant_outputs) <> 0
     OR (SELECT count(*) FROM company.secrets) <> 1 THEN
    RAISE EXCEPTION 'other owner reached the credential or output';
  END IF;
  SELECT r.decision INTO decision
    FROM company.get_isolated_output(assistant_a, secret_a, 'fixture', output_a) r;
  IF decision IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'other owner read the bound output';
  END IF;
  -- Mixing one owner's assistant with another owner's credential is denied.
  IF (SELECT r.decision FROM company.record_isolated_output(
        assistant_b, secret_a, 'fixture', 'mixed') r) IS DISTINCT FROM 'denied'
     OR (SELECT r.decision FROM company.record_isolated_output(
        assistant_a, secret_b, 'fixture', 'mixed') r) IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'cross-owner binding was accepted';
  END IF;
  -- Direct SQL cannot attach a foreign credential to an output.
  rejected := false;
  BEGIN
    INSERT INTO company.assistant_outputs(org_id, assistant_id, owner_id, namespace,
      secret_id, provider, body)
    VALUES (org_a, assistant_b, owner_b, namespace_b, secret_a, 'fixture', 'forged');
  EXCEPTION WHEN foreign_key_violation THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'foreign credential binding was accepted'; END IF;

  -- Direct SQL cannot re-point a bound output. Outputs have no UPDATE policy, so
  -- FORCE RLS blocks the write and the binding stays intact.
  PERFORM set_config('company.member_id', owner_a::text, true);
  UPDATE company.assistant_outputs SET provider = 'other' WHERE id = output_a;
  IF (SELECT o.provider FROM company.assistant_outputs o WHERE o.id = output_a)
       IS DISTINCT FROM 'fixture' THEN
    RAISE EXCEPTION 'bound output was rebound';
  END IF;

  -- Revoking the credential revokes the identity binding immediately.
  IF company.revoke_personal_secret(secret_a) <> 'revoked' THEN
    RAISE EXCEPTION 'credential revocation failed';
  END IF;
  IF company.identity_isolation_decision(assistant_a, secret_a, 'fixture', output_a)
     IS DISTINCT FROM 'denied'
     OR (SELECT r.decision FROM company.get_isolated_output(
          assistant_a, secret_a, 'fixture', output_a) r) IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'revoked credential kept the identity binding';
  END IF;

  -- Revoking the assistant revokes its credentials and identity bindings.
  SELECT r.secret_id INTO secret_a2
    FROM company.store_personal_secret('fixture2', 'personal', next_envelope, old_nonce, 1) r;
  SELECT r.output_id INTO output_a2
    FROM company.record_isolated_output(assistant_a, secret_a2, 'fixture2', 'answer-a2') r;
  IF secret_a2 IS NULL OR output_a2 IS NULL
     OR company.identity_isolation_decision(assistant_a, secret_a2, 'fixture2', output_a2)
        IS DISTINCT FROM 'allow' THEN
    RAISE EXCEPTION 'second bound output fixture failed';
  END IF;
  revoke_decision := company.revoke_personal_assistant(assistant_a);
  IF revoke_decision <> 'revoked'
     OR company.identity_isolation_decision(assistant_a, secret_a2, 'fixture2', output_a2)
        IS DISTINCT FROM 'denied'
     OR (SELECT r.decision FROM company.get_isolated_output(
          assistant_a, secret_a2, 'fixture2', output_a2) r) IS DISTINCT FROM 'denied'
     OR (SELECT count(*) FROM company.assistant_outputs) <> 0 THEN
    RAISE EXCEPTION 'assistant revocation left the identity binding active';
  END IF;

  -- A separate tenant stays isolated.
  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', owner_c::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_b, 'c06-' || org_b::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state)
    VALUES (owner_c, org_b, 'c06-' || owner_c::text, 'admin', 'active');
  SELECT r.assistant_id INTO assistant_c FROM company.register_personal_assistant() r;
  SELECT r.secret_id INTO secret_c
    FROM company.store_personal_secret('fixture', 'personal', old_envelope, old_nonce, 1) r;
  IF assistant_c IS NULL OR secret_c IS NULL
     OR company.identity_isolation_decision(assistant_c, secret_c, 'fixture', output_a)
        IS DISTINCT FROM 'denied'
     OR company.identity_isolation_decision(assistant_a, secret_a, 'fixture', output_a)
        IS DISTINCT FROM 'denied'
     OR (SELECT count(*) FROM company.secrets) <> 1 THEN
    RAISE EXCEPTION 'tenant identity isolation failed';
  END IF;
END;
$verify$;
ROLLBACK;
