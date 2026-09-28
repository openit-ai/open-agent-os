-- C05 keeps only encrypted envelopes in PostgreSQL. The trusted caller seals
-- them with AES-256-GCM outside the database and sets authenticated GUCs.
ALTER TABLE company.secrets ADD COLUMN generation integer NOT NULL DEFAULT 1
  CHECK (generation > 0);
ALTER TABLE company.secrets ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE company.secrets ADD COLUMN rotated_at timestamptz;
ALTER TABLE company.secrets ADD COLUMN revoked_at timestamptz;

-- C01 allowed organization rows and inactive owners. Personal credentials
-- now require an active member and an active personal assistant at every read.
DROP POLICY secret_scope ON company.secrets;
CREATE FUNCTION company.c05_secret_owner_ready(p_org uuid, p_owner uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT p_org::text = current_setting('company.org_id', true)
    AND p_owner::text = current_setting('company.member_id', true)
    AND company.c04_member_ready()
    AND EXISTS (SELECT 1 FROM company.assistants a
      WHERE a.org_id = p_org AND a.owner_id = p_owner AND a.state = 'active');
$$;
CREATE POLICY secret_read ON company.secrets FOR SELECT USING (
  owner_id IS NOT NULL
  AND company.c05_secret_owner_ready(org_id, owner_id));
CREATE POLICY secret_insert ON company.secrets FOR INSERT WITH CHECK (
  state = 'active' AND owner_id IS NOT NULL
  AND company.c05_secret_owner_ready(org_id, owner_id));
CREATE POLICY secret_update ON company.secrets FOR UPDATE USING (
  state = 'active' AND owner_id IS NOT NULL
  AND company.c05_secret_owner_ready(org_id, owner_id)) WITH CHECK (
  owner_id IS NOT NULL AND company.c05_secret_owner_ready(org_id, owner_id));

-- The trigger also covers direct app-role SQL, so a rotation cannot silently
-- replace a credential without incrementing its generation and audit trail.
CREATE FUNCTION company.c05_secret_lifecycle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.owner_id IS NULL OR NEW.state <> 'active' OR NEW.generation <> 1 THEN
      RAISE EXCEPTION 'personal secret requires an active owner and first generation';
    END IF;
    -- Serialize storage with assistant revocation, including direct app SQL.
    PERFORM 1 FROM company.assistants a
      WHERE a.org_id = NEW.org_id AND a.owner_id = NEW.owner_id
        AND a.state = 'active' FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'active personal assistant required'; END IF;
    RETURN NEW;
  END IF;
  IF OLD.state <> 'active' OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
     OR NEW.provider IS DISTINCT FROM OLD.provider
     OR NEW.purpose IS DISTINCT FROM OLD.purpose
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'secret binding and revocation are immutable';
  END IF;
  IF NEW.state = 'revoked' THEN
    NEW.ciphertext := decode('00', 'hex');
    NEW.nonce := decode('00', 'hex');
    NEW.key_version := OLD.key_version;
    NEW.generation := OLD.generation + 1;
    NEW.rotated_at := OLD.rotated_at;
    NEW.revoked_at := now();
  ELSIF NEW.state = 'active' AND NEW.ciphertext IS DISTINCT FROM OLD.ciphertext
        AND NEW.nonce IS DISTINCT FROM OLD.nonce AND NEW.revoked_at IS NULL
        AND NEW.generation = OLD.generation THEN
    NEW.generation := OLD.generation + 1;
    NEW.rotated_at := now();
  ELSE
    RAISE EXCEPTION 'secret rotation requires fresh ciphertext and nonce';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER secrets_c05_lifecycle BEFORE INSERT OR UPDATE ON company.secrets
  FOR EACH ROW EXECUTE FUNCTION company.c05_secret_lifecycle();

-- The C01 audit policy permitted mutation. C05 records credential changes in
-- append-only rows without an encrypted envelope, key, or token in metadata.
DROP POLICY audit_scope ON company.audit_events;
CREATE POLICY audit_read ON company.audit_events FOR SELECT USING (
  org_id::text = current_setting('company.org_id', true)
  AND (company.c03_admin_ready()
       OR actor_id::text = current_setting('company.member_id', true)
       OR owner_id::text = current_setting('company.member_id', true)));
CREATE POLICY audit_insert ON company.audit_events FOR INSERT WITH CHECK (
  org_id::text = current_setting('company.org_id', true)
  AND actor_id::text = current_setting('company.member_id', true));
CREATE FUNCTION company.c05_audit_secret_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE event_action text;
BEGIN
  IF TG_OP = 'INSERT' THEN event_action := 'stored';
  ELSIF NEW.state = 'revoked' THEN event_action := 'revoked';
  ELSE event_action := 'rotated'; END IF;
  INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type,
    action, resource_ref, decision, request_id, metadata)
  VALUES (NEW.org_id, NEW.owner_id, NEW.owner_id, 'personal_credential',
    event_action, NEW.id::text, 'allow', gen_random_uuid()::text,
    jsonb_build_object('generation', NEW.generation, 'key_version', NEW.key_version));
  RETURN NEW;
END;
$$;
CREATE TRIGGER secrets_c05_audit AFTER INSERT OR UPDATE ON company.secrets
  FOR EACH ROW EXECUTE FUNCTION company.c05_audit_secret_change();

-- Replacing a revoked assistant must never revive credentials from its former
-- lifetime. Revoke while the old assistant is still active under FORCE RLS.
CREATE FUNCTION company.c05_revoke_assistant_secrets() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state = 'active' AND NEW.state <> 'active' THEN
    UPDATE company.secrets SET state = 'revoked'
      WHERE org_id = OLD.org_id AND owner_id = OLD.owner_id AND state = 'active';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER assistants_c05_revoke_secrets BEFORE UPDATE OF state
  ON company.assistants FOR EACH ROW
  EXECUTE FUNCTION company.c05_revoke_assistant_secrets();

CREATE FUNCTION company.store_personal_secret(p_provider text, p_purpose text,
  p_ciphertext bytea, p_nonce bytea, p_key_version integer)
RETURNS TABLE(decision text, secret_id uuid, secret_generation integer)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE new_secret company.secrets%ROWTYPE;
BEGIN
  IF NOT company.c05_secret_owner_ready(
       NULLIF(current_setting('company.org_id', true), '')::uuid,
       NULLIF(current_setting('company.member_id', true), '')::uuid)
     OR p_provider IS NULL OR p_provider = ''
     OR p_purpose IS NULL OR p_purpose = '' OR p_ciphertext IS NULL
     OR length(p_ciphertext) <= 16 OR p_nonce IS NULL OR length(p_nonce) <> 12
     OR p_key_version IS NULL OR p_key_version <= 0 THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::integer; RETURN;
  END IF;
  BEGIN
    INSERT INTO company.secrets(org_id, owner_id, provider, purpose,
      ciphertext, nonce, key_version)
    VALUES (current_setting('company.org_id', true)::uuid,
      current_setting('company.member_id', true)::uuid, p_provider, p_purpose,
      p_ciphertext, p_nonce, p_key_version) RETURNING * INTO new_secret;
  EXCEPTION WHEN unique_violation THEN
    RETURN QUERY SELECT 'duplicate'::text, NULL::uuid, NULL::integer; RETURN;
  END;
  RETURN QUERY SELECT 'stored'::text, new_secret.id, new_secret.generation;
END;
$$;

CREATE FUNCTION company.fetch_personal_secret_envelope(p_secret uuid, p_assistant uuid)
RETURNS TABLE(decision text, provider text, purpose text, ciphertext bytea,
  nonce bytea, key_version integer, secret_generation integer)
LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
  IF company.assistant_execution_decision(p_assistant) = 'allow' THEN
    RETURN QUERY SELECT 'found'::text, s.provider, s.purpose, s.ciphertext,
      s.nonce, s.key_version, s.generation FROM company.secrets s
      WHERE s.id = p_secret AND s.state = 'active';
    IF FOUND THEN RETURN; END IF;
  END IF;
  RETURN QUERY SELECT 'denied'::text, NULL::text, NULL::text, NULL::bytea,
    NULL::bytea, NULL::integer, NULL::integer;
END;
$$;

CREATE FUNCTION company.personal_secret_use_decision(p_secret uuid,
  p_assistant uuid, p_generation integer) RETURNS text
LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT CASE WHEN company.assistant_execution_decision(p_assistant) = 'allow'
    AND EXISTS (SELECT 1 FROM company.secrets s WHERE s.id = p_secret
      AND s.state = 'active' AND s.generation = p_generation)
    THEN 'allow' ELSE 'denied' END;
$$;

CREATE FUNCTION company.rotate_personal_secret(p_secret uuid, p_ciphertext bytea,
  p_nonce bytea, p_key_version integer)
RETURNS TABLE(decision text, secret_generation integer)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE next_generation integer;
BEGIN
  IF NOT company.c04_member_ready() OR p_ciphertext IS NULL
     OR length(p_ciphertext) <= 16 OR p_nonce IS NULL OR length(p_nonce) <> 12
     OR p_key_version IS NULL OR p_key_version <= 0 THEN
    RETURN QUERY SELECT 'denied'::text, NULL::integer; RETURN;
  END IF;
  UPDATE company.secrets s SET ciphertext = p_ciphertext, nonce = p_nonce,
    key_version = p_key_version WHERE s.id = p_secret AND s.state = 'active'
    RETURNING s.generation INTO next_generation;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'denied'::text, NULL::integer; RETURN;
  END IF;
  RETURN QUERY SELECT 'rotated'::text, next_generation;
END;
$$;

CREATE FUNCTION company.revoke_personal_secret(p_secret uuid) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
BEGIN
  IF NOT company.c04_member_ready() THEN RETURN 'denied'; END IF;
  UPDATE company.secrets s SET state = 'revoked'
    WHERE s.id = p_secret AND s.state = 'active';
  IF FOUND THEN RETURN 'revoked'; END IF;
  RETURN 'denied';
END;
$$;
