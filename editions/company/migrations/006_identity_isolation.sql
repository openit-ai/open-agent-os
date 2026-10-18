-- C06 enforces the Company identity invariant
--   owner == credential == provider == output
-- for a personal assistant. The trusted server sets company.org_id/member_id from
-- the authenticated Project session; every personal row must share that owner.
--
-- C04 recorded plain outputs and C05 stored encrypted personal credentials. C06
-- binds an output to the exact credential and provider that produced it so a
-- caller cannot combine another owner's assistant, credential or output, nor a
-- credential from a different provider than the request claims.

-- A composite key lets the output FK prove owner and provider in one step.
-- owner_id and provider are immutable on secrets (C01/C05 triggers), so this key
-- is stable for the lifetime of the credential.
ALTER TABLE company.secrets ADD CONSTRAINT secrets_identity_key
  UNIQUE (org_id, id, owner_id, provider);

-- Bound outputs reference their credential. Legacy C04 outputs keep NULL binding
-- and stay readable through the old API; the identity guard denies them until
-- they are re-recorded with a credential and provider.
ALTER TABLE company.assistant_outputs ADD COLUMN secret_id uuid;
ALTER TABLE company.assistant_outputs ADD COLUMN provider text;
ALTER TABLE company.assistant_outputs ADD CONSTRAINT assistant_outputs_binding_check
  CHECK ((secret_id IS NULL) = (provider IS NULL));
ALTER TABLE company.assistant_outputs ADD CONSTRAINT assistant_outputs_credential_fk
  FOREIGN KEY (org_id, secret_id, owner_id, provider)
  REFERENCES company.secrets(org_id, id, owner_id, provider);

-- One owner, one active assistant, one active credential for that owner/provider,
-- and one output already bound to that same credential. Foreign IDs are returned
-- as a plain denial, never as existence.
CREATE FUNCTION company.c06_identity_ready(p_org uuid, p_owner uuid, p_assistant uuid,
  p_secret uuid, p_provider text, p_output uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT p_org::text = current_setting('company.org_id', true)
    AND p_owner::text = current_setting('company.member_id', true)
    AND p_provider IS NOT NULL AND p_provider <> ''
    AND company.c04_member_ready()
    AND EXISTS (SELECT 1 FROM company.assistants a
      WHERE a.org_id = p_org AND a.id = p_assistant AND a.owner_id = p_owner
        AND a.state = 'active')
    AND EXISTS (SELECT 1 FROM company.secrets s
      WHERE s.org_id = p_org AND s.id = p_secret AND s.owner_id = p_owner
        AND s.provider = p_provider AND s.state = 'active')
    AND EXISTS (SELECT 1 FROM company.assistant_outputs o
      WHERE o.org_id = p_org AND o.id = p_output AND o.owner_id = p_owner
        AND o.assistant_id = p_assistant AND o.secret_id = p_secret
        AND o.provider = p_provider);
$$;

CREATE FUNCTION company.identity_isolation_decision(p_assistant uuid, p_secret uuid,
  p_provider text, p_output uuid) RETURNS text
LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT CASE WHEN company.c06_identity_ready(
      NULLIF(current_setting('company.org_id', true), '')::uuid,
      NULLIF(current_setting('company.member_id', true), '')::uuid,
      p_assistant, p_secret, p_provider, p_output)
    THEN 'allow' ELSE 'denied' END;
$$;

-- Recording derives the owner from the verified session and binds the output to
-- the owner's active credential for the claimed provider. A foreign assistant or
-- credential fails the same way an unknown one does.
CREATE FUNCTION company.record_isolated_output(p_assistant uuid, p_secret uuid,
  p_provider text, p_body text)
RETURNS TABLE(decision text, output_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE a company.assistants%ROWTYPE;
DECLARE s company.secrets%ROWTYPE;
DECLARE new_id uuid;
BEGIN
  IF NOT company.c04_member_ready()
     OR p_provider IS NULL OR p_provider = ''
     OR p_body IS NULL THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  SELECT * INTO a FROM company.assistants
    WHERE org_id::text = current_setting('company.org_id', true)
      AND id = p_assistant
      AND owner_id::text = current_setting('company.member_id', true)
      AND state = 'active' FOR SHARE;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  SELECT * INTO s FROM company.secrets
    WHERE org_id = a.org_id AND id = p_secret AND owner_id = a.owner_id
      AND provider = p_provider AND state = 'active' FOR SHARE;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  INSERT INTO company.assistant_outputs(org_id, assistant_id, owner_id, namespace,
    secret_id, provider, body)
  VALUES (a.org_id, a.id, a.owner_id, a.knowledge_namespace, s.id, s.provider, p_body)
  RETURNING id INTO new_id;
  RETURN QUERY SELECT 'created'::text, new_id;
END;
$$;

CREATE FUNCTION company.get_isolated_output(p_assistant uuid, p_secret uuid,
  p_provider text, p_output uuid)
RETURNS TABLE(decision text, body text)
LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
  IF company.identity_isolation_decision(p_assistant, p_secret, p_provider, p_output)
     = 'allow' THEN
    RETURN QUERY SELECT 'found'::text, o.body FROM company.assistant_outputs o
      WHERE o.id = p_output AND o.assistant_id = p_assistant
        AND o.secret_id = p_secret AND o.provider = p_provider;
    IF FOUND THEN RETURN; END IF;
  END IF;
  RETURN QUERY SELECT 'denied'::text, NULL::text;
END;
$$;

-- Direct SQL must not re-point a bound output to another assistant, owner,
-- namespace, credential or provider. NULL-to-bound promotion is allowed once so
-- legacy outputs can be re-recorded, but a bound row is immutable.
CREATE FUNCTION company.c06_output_binding_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.assistant_id IS DISTINCT FROM OLD.assistant_id
     OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
     OR NEW.namespace IS DISTINCT FROM OLD.namespace
     OR OLD.secret_id IS NOT NULL
        AND (NEW.secret_id IS DISTINCT FROM OLD.secret_id
             OR NEW.provider IS DISTINCT FROM OLD.provider) THEN
    RAISE EXCEPTION 'assistant output identity binding is immutable';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER assistant_outputs_c06_binding BEFORE UPDATE OF assistant_id, owner_id,
  namespace, secret_id, provider ON company.assistant_outputs
  FOR EACH ROW EXECUTE FUNCTION company.c06_output_binding_immutable();

-- Binding a credential to an output is audited without the credential material.
CREATE FUNCTION company.c06_audit_output_binding() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.secret_id IS NOT NULL THEN
    INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
      resource_ref, decision, request_id, metadata)
    VALUES (NEW.org_id, NEW.owner_id, NEW.owner_id, 'identity_binding', 'output_bound',
      NEW.id::text, 'allow', gen_random_uuid()::text,
      jsonb_build_object('assistant_id', NEW.assistant_id, 'secret_id', NEW.secret_id,
        'provider', NEW.provider));
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER assistant_outputs_c06_audit AFTER INSERT ON company.assistant_outputs
  FOR EACH ROW EXECUTE FUNCTION company.c06_audit_output_binding();
