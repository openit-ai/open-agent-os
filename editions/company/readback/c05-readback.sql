-- SPDX-License-Identifier: BUSL-1.1
-- Company edition: see LICENSE-COMPANY at the repository root.
-- C05 uses only synthetic encrypted envelopes; the transaction always rolls back.
BEGIN;
DO $verify$
DECLARE
  org_a uuid := gen_random_uuid();
  org_b uuid := gen_random_uuid();
  owner_a uuid := gen_random_uuid();
  owner_b uuid := gen_random_uuid();
  owner_c uuid := gen_random_uuid();
  owner_d uuid := gen_random_uuid();
  owner_e uuid := gen_random_uuid();
  assistant_d uuid;
  assistant_e uuid;
  secret_d uuid;
  secret_e uuid;
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
  -- The session administrator bootstraps the organization first (019).
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (owner_a, org_a, 'c05-' || owner_a::text, 'admin', 'active');
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
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
  -- 024: key rotation functions are maintenance-only, and a runtime session
  -- that sets the re-encryption marker itself still performs an ordinary
  -- rotation (new generation, 'rotated' audit), never a silent re-encryption.
  IF has_function_privilege('company.vault_rekey_secret(uuid, integer, bytea, bytea, bytea, integer)', 'EXECUTE')
     OR has_function_privilege('company.vault_key_inventory(integer, integer, uuid)', 'EXECUTE')
     OR has_function_privilege('company.vault_key_usage()', 'EXECUTE') THEN
    RAISE EXCEPTION 'runtime may run the vault key rotation';
  END IF;
  PERFORM set_config('company.vault_rekey', 'on', true);
  UPDATE company.secrets SET ciphertext = old_envelope, nonce = old_nonce, key_version = 3
   WHERE id = secret_a;
  PERFORM set_config('company.vault_rekey', 'off', true);
  IF (SELECT generation FROM company.secrets WHERE id = secret_a) <> generation_a + 2
     OR (SELECT count(*) FROM company.audit_events
          WHERE resource_ref = secret_a::text AND action = 'rekeyed') <> 0 THEN
    RAISE EXCEPTION 'runtime re-encrypted a credential without a new generation';
  END IF;
  -- The runtime role has no UPDATE on audit rows (migration 013); a
  -- permission error is the expected append-only answer.
  changed := 0;
  BEGIN
    UPDATE company.audit_events SET decision = 'changed'
    WHERE event_type = 'personal_credential' AND resource_ref = secret_a::text;
    GET DIAGNOSTICS changed = ROW_COUNT;
  EXCEPTION WHEN insufficient_privilege THEN changed := 0;
  END;
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

  -- Migration 014: suspension and departure revoke assistants, credential
  -- envelopes, and external accounts independent of the route taken.
  PERFORM set_config('company.member_id', owner_a::text, true);
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (owner_d, org_a, 'c05-' || owner_d::text, 'member', 'active'),
    (owner_e, org_a, 'c05-' || owner_e::text, 'member', 'active');
  INSERT INTO company.external_accounts(org_id, member_id, provider, workspace_id,
    subject_id, state) VALUES (org_a, owner_d, 'slack', 'c05-ws', 'c05-' || owner_d::text, 'active');
  PERFORM set_config('company.member_id', owner_d::text, true);
  SELECT r.assistant_id INTO assistant_d FROM company.register_personal_assistant() r;
  SELECT r.secret_id INTO secret_d
    FROM company.store_personal_secret('fixture', 'personal', old_envelope, old_nonce, 1) r;
  PERFORM set_config('company.member_id', owner_e::text, true);
  SELECT r.assistant_id INTO assistant_e FROM company.register_personal_assistant() r;
  SELECT r.secret_id INTO secret_e
    FROM company.store_personal_secret('fixture', 'personal', old_envelope, old_nonce, 1) r;
  IF secret_d IS NULL OR secret_e IS NULL THEN RAISE EXCEPTION 'revocation fixtures missing'; END IF;
  PERFORM set_config('company.member_id', owner_a::text, true);
  UPDATE company.members SET state = 'suspended' WHERE id = owner_d;
  UPDATE company.members SET state = 'departed' WHERE id = owner_e;
  IF current_setting('company.member_id', true) IS DISTINCT FROM owner_a::text THEN
    RAISE EXCEPTION 'revocation trigger did not restore the caller context';
  END IF;
  IF (SELECT count(*) FROM company.external_accounts
       WHERE member_id = owner_d AND state <> 'revoked') <> 0 THEN
    RAISE EXCEPTION 'suspension left an external account active';
  END IF;
  IF (SELECT count(*) FROM company.audit_events
       WHERE action = 'revoked' AND resource_ref IN (secret_d::text, secret_e::text)) <> 2 THEN
    RAISE EXCEPTION 'suspension or departure left a credential active';
  END IF;
  -- A reactivated member finds the old assistant and credential revoked.
  UPDATE company.members SET state = 'active' WHERE id = owner_d;
  PERFORM set_config('company.member_id', owner_d::text, true);
  IF company.assistant_execution_decision(assistant_d) <> 'denied'
     OR company.personal_secret_use_decision(secret_d, assistant_d, 1) <> 'denied' THEN
    RAISE EXCEPTION 'reactivated member regained a suspended assistant or credential';
  END IF;
END;
$verify$;
ROLLBACK;
