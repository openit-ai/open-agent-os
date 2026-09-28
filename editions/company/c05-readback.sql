-- C05 uses only synthetic encrypted envelopes; the transaction always rolls back.
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
  secret_a uuid;
  secret_b uuid;
  secret_c uuid;
  generation_a integer;
  returned_generation integer;
  key_version integer;
  decision text;
  provider text;
  purpose text;
  ciphertext bytea;
  nonce bytea;
  old_envelope bytea := decode(repeat('ab', 40), 'hex');
  next_envelope bytea := decode(repeat('cd', 40), 'hex');
  old_nonce bytea := decode(repeat('01', 12), 'hex');
  next_nonce bytea := decode(repeat('02', 12), 'hex');
  changed integer;
BEGIN
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', owner_a::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_a, 'c05-' || org_a::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (owner_a, org_a, 'c05-' || owner_a::text, 'admin', 'active'),
    (owner_b, org_a, 'c05-' || owner_b::text, 'member', 'active');
  SELECT r.assistant_id INTO assistant_a FROM company.register_personal_assistant() r;
  IF assistant_a IS NULL THEN RAISE EXCEPTION 'first assistant missing'; END IF;
  SELECT r.decision, r.secret_id, r.secret_generation
    INTO decision, secret_a, generation_a
    FROM company.store_personal_secret('fixture', 'personal', old_envelope, old_nonce, 1) r;
  IF decision IS DISTINCT FROM 'stored' OR secret_a IS NULL OR generation_a <> 1 THEN
    RAISE EXCEPTION 'personal credential was not stored';
  END IF;
  SELECT r.decision, r.provider, r.purpose, r.ciphertext, r.nonce,
         r.key_version, r.secret_generation
    INTO decision, provider, purpose, ciphertext, nonce, key_version, returned_generation
    FROM company.fetch_personal_secret_envelope(secret_a, assistant_a) r;
  IF decision IS DISTINCT FROM 'found' OR provider IS DISTINCT FROM 'fixture'
     OR purpose IS DISTINCT FROM 'personal' OR ciphertext IS DISTINCT FROM old_envelope
     OR nonce IS DISTINCT FROM old_nonce OR key_version <> 1
     OR returned_generation <> generation_a THEN
    RAISE EXCEPTION 'owner encrypted envelope read failed';
  END IF;
  IF company.personal_secret_use_decision(secret_a, assistant_a, generation_a)
     IS DISTINCT FROM 'allow' THEN
    RAISE EXCEPTION 'owner credential use denied';
  END IF;
  IF (SELECT count(*) FROM company.audit_events
      WHERE event_type = 'personal_credential' AND action = 'stored'
        AND resource_ref = secret_a::text) <> 1 THEN
    RAISE EXCEPTION 'storage audit event missing';
  END IF;

  PERFORM set_config('company.member_id', owner_b::text, true);
  SELECT r.assistant_id INTO assistant_b FROM company.register_personal_assistant() r;
  IF assistant_b IS NULL THEN RAISE EXCEPTION 'second assistant missing'; END IF;
  SELECT r.decision, r.ciphertext INTO decision, ciphertext
    FROM company.fetch_personal_secret_envelope(secret_a, assistant_b) r;
  IF decision IS DISTINCT FROM 'denied' OR ciphertext IS NOT NULL
     OR company.personal_secret_use_decision(secret_a, assistant_b, generation_a) <> 'denied'
     OR (SELECT count(*) FROM company.secrets WHERE state = 'active') <> 0 THEN
    RAISE EXCEPTION 'other owner accessed credential';
  END IF;
  SELECT r.decision INTO decision
    FROM company.rotate_personal_secret(secret_a, next_envelope, next_nonce, 1) r;
  IF decision IS DISTINCT FROM 'denied'
     OR company.revoke_personal_secret(secret_a) <> 'denied' THEN
    RAISE EXCEPTION 'other owner changed credential';
  END IF;
  SELECT r.decision, r.secret_id INTO decision, secret_b
    FROM company.store_personal_secret('fixture', 'personal', old_envelope, old_nonce, 1) r;
  IF decision IS DISTINCT FROM 'stored' OR secret_b IS NULL THEN
    RAISE EXCEPTION 'second owner credential was not stored';
  END IF;

  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', owner_c::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_b, 'c05-' || org_b::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state)
    VALUES (owner_c, org_b, 'c05-' || owner_c::text, 'admin', 'active');
  SELECT r.assistant_id INTO assistant_c FROM company.register_personal_assistant() r;
  SELECT r.secret_id INTO secret_c
    FROM company.store_personal_secret('fixture', 'personal', old_envelope, old_nonce, 1) r;
  IF assistant_c IS NULL OR secret_c IS NULL
     OR company.personal_secret_use_decision(secret_a, assistant_c, generation_a) <> 'denied'
     OR (SELECT count(*) FROM company.secrets) <> 1 THEN
    RAISE EXCEPTION 'tenant credential isolation failed';
  END IF;

  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', owner_a::text, true);
  SELECT r.decision, r.secret_generation INTO decision, returned_generation
    FROM company.rotate_personal_secret(secret_a, next_envelope, next_nonce, 2) r;
  IF decision IS DISTINCT FROM 'rotated' OR returned_generation <> generation_a + 1
     OR company.personal_secret_use_decision(secret_a, assistant_a, generation_a) <> 'denied'
     OR company.personal_secret_use_decision(secret_a, assistant_a, returned_generation) <> 'allow' THEN
    RAISE EXCEPTION 'old credential generation remained usable';
  END IF;
  SELECT r.decision, r.ciphertext, r.nonce, r.key_version
    INTO decision, ciphertext, nonce, key_version
    FROM company.fetch_personal_secret_envelope(secret_a, assistant_a) r;
  IF decision IS DISTINCT FROM 'found' OR ciphertext IS DISTINCT FROM next_envelope
     OR ciphertext = old_envelope OR nonce IS DISTINCT FROM next_nonce OR key_version <> 2 THEN
    RAISE EXCEPTION 'rotation did not replace encrypted envelope';
  END IF;
  IF (SELECT count(*) FROM company.audit_events
      WHERE event_type = 'personal_credential' AND action = 'rotated'
        AND resource_ref = secret_a::text AND metadata->>'generation' = '2') <> 1 THEN
    RAISE EXCEPTION 'rotation audit event missing';
  END IF;
  UPDATE company.audit_events SET decision = 'changed'
    WHERE event_type = 'personal_credential' AND resource_ref = secret_a::text;
  GET DIAGNOSTICS changed = ROW_COUNT;
  IF changed <> 0 THEN RAISE EXCEPTION 'audit history was mutable'; END IF;

  IF company.revoke_personal_secret(secret_a) <> 'revoked' THEN
    RAISE EXCEPTION 'credential revocation failed';
  END IF;
  IF company.revoke_personal_secret(secret_a) <> 'denied'
     OR company.personal_secret_use_decision(secret_a, assistant_a, returned_generation) <> 'denied'
     OR (SELECT count(*) FROM company.secrets WHERE state = 'active') <> 0 THEN
    RAISE EXCEPTION 'revoked credential remained usable';
  END IF;
  IF (SELECT count(*) FROM company.secrets s
      WHERE s.id = secret_a AND s.state = 'revoked'
        AND s.ciphertext = decode('00', 'hex') AND s.nonce = decode('00', 'hex')) <> 1 THEN
    RAISE EXCEPTION 'revoked encrypted envelope was not erased';
  END IF;
  SELECT r.decision, r.ciphertext INTO decision, ciphertext
    FROM company.fetch_personal_secret_envelope(secret_a, assistant_a) r;
  IF decision IS DISTINCT FROM 'denied' OR ciphertext IS NOT NULL THEN
    RAISE EXCEPTION 'revoked credential remained readable';
  END IF;
  IF (SELECT count(*) FROM company.audit_events
      WHERE event_type = 'personal_credential' AND action = 'revoked'
        AND resource_ref = secret_a::text) <> 1 THEN
    RAISE EXCEPTION 'revocation audit event missing';
  END IF;
  SELECT r.decision INTO decision
    FROM company.rotate_personal_secret(secret_a, old_envelope, old_nonce, 1) r;
  IF decision IS DISTINCT FROM 'denied' THEN RAISE EXCEPTION 'revoked credential rotated'; END IF;
  PERFORM set_config('company.member_id', owner_b::text, true);
  IF company.revoke_personal_assistant(assistant_b) <> 'revoked' THEN
    RAISE EXCEPTION 'second assistant revocation failed';
  END IF;
  IF company.personal_secret_use_decision(secret_b, assistant_b, 1) <> 'denied'
     OR (SELECT count(*) FROM company.audit_events
         WHERE action = 'revoked' AND resource_ref = secret_b::text) <> 1 THEN
    RAISE EXCEPTION 'assistant revocation left credential active';
  END IF;
  SELECT r.decision INTO decision
    FROM company.store_personal_secret('fixture', 'personal', old_envelope, old_nonce, 1) r;
  IF decision IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'revoked assistant stored credential';
  END IF;
  SELECT r.assistant_id INTO assistant_b FROM company.register_personal_assistant() r;
  IF assistant_b IS NULL OR company.personal_secret_use_decision(secret_b, assistant_b, 1) <> 'denied' THEN
    RAISE EXCEPTION 'replacement assistant reused old credential';
  END IF;
END;
$verify$;
ROLLBACK;
