-- C04 operations run as the authenticated caller under C01 FORCE RLS.
-- The trusted server sets company.org_id/member_id for each transaction.
CREATE FUNCTION company.c04_member_ready() RETURNS boolean
LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT EXISTS (
    SELECT 1 FROM company.members m JOIN company.organizations o ON o.id = m.org_id
    WHERE m.org_id::text = current_setting('company.org_id', true)
      AND m.id::text = current_setting('company.member_id', true)
      AND m.state = 'active' AND o.state = 'active'
  );
$$;

-- A revoked assistant cannot be revived or moved to another namespace.
CREATE FUNCTION company.c04_assistant_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.knowledge_namespace IS DISTINCT FROM OLD.knowledge_namespace
     OR (OLD.state = 'revoked' AND NEW.state <> 'revoked') THEN
    RAISE EXCEPTION 'assistant namespace and revocation are immutable';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER assistants_c04_immutable BEFORE UPDATE OF knowledge_namespace, state
  ON company.assistants FOR EACH ROW EXECUTE FUNCTION company.c04_assistant_immutable();

-- C01's owner-only policy is tightened to active-member access. Revoked
-- metadata remains visible to its owner for history; content and API reads do not.
DROP POLICY assistant_scope ON company.assistants;
CREATE POLICY assistant_read ON company.assistants FOR SELECT USING (
  org_id::text = current_setting('company.org_id', true)
  AND owner_id::text = current_setting('company.member_id', true)
  AND company.c04_member_ready()
);
CREATE POLICY assistant_insert ON company.assistants FOR INSERT WITH CHECK (
  org_id::text = current_setting('company.org_id', true)
  AND owner_id::text = current_setting('company.member_id', true)
  AND state = 'active' AND company.c04_member_ready()
);
CREATE POLICY assistant_update ON company.assistants FOR UPDATE USING (
  org_id::text = current_setting('company.org_id', true)
  AND owner_id::text = current_setting('company.member_id', true)
  AND state = 'active' AND company.c04_member_ready()
) WITH CHECK (
  org_id::text = current_setting('company.org_id', true)
  AND owner_id::text = current_setting('company.member_id', true)
  AND company.c04_member_ready()
);

ALTER TABLE company.assistants ADD CONSTRAINT assistants_namespace_owner_key
  UNIQUE (org_id, id, owner_id, knowledge_namespace);

CREATE TABLE company.assistant_knowledge (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL,
  assistant_id uuid NOT NULL,
  owner_id uuid NOT NULL,
  namespace uuid NOT NULL,
  body text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, assistant_id, owner_id, namespace)
    REFERENCES company.assistants(org_id, id, owner_id, knowledge_namespace)
);
CREATE TABLE company.assistant_outputs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL,
  assistant_id uuid NOT NULL,
  owner_id uuid NOT NULL,
  namespace uuid NOT NULL,
  body text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, assistant_id, owner_id, namespace)
    REFERENCES company.assistants(org_id, id, owner_id, knowledge_namespace)
);

CREATE FUNCTION company.c04_private_row_ready(p_org uuid, p_owner uuid,
  p_assistant uuid, p_namespace uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT p_org::text = current_setting('company.org_id', true)
    AND p_owner::text = current_setting('company.member_id', true)
    AND company.c04_member_ready()
    AND EXISTS (SELECT 1 FROM company.assistants a
      WHERE a.org_id = p_org AND a.id = p_assistant AND a.owner_id = p_owner
        AND a.knowledge_namespace = p_namespace AND a.state = 'active');
$$;
ALTER TABLE company.assistant_knowledge ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.assistant_knowledge FORCE ROW LEVEL SECURITY;
ALTER TABLE company.assistant_outputs ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.assistant_outputs FORCE ROW LEVEL SECURITY;
CREATE POLICY knowledge_read ON company.assistant_knowledge FOR SELECT USING (
  company.c04_private_row_ready(org_id, owner_id, assistant_id, namespace));
CREATE POLICY knowledge_insert ON company.assistant_knowledge FOR INSERT WITH CHECK (
  company.c04_private_row_ready(org_id, owner_id, assistant_id, namespace));
CREATE POLICY output_read ON company.assistant_outputs FOR SELECT USING (
  company.c04_private_row_ready(org_id, owner_id, assistant_id, namespace));
CREATE POLICY output_insert ON company.assistant_outputs FOR INSERT WITH CHECK (
  company.c04_private_row_ready(org_id, owner_id, assistant_id, namespace));

CREATE FUNCTION company.register_personal_assistant()
RETURNS TABLE(decision text, assistant_id uuid, knowledge_namespace uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE existing company.assistants%ROWTYPE;
BEGIN
  IF NOT company.c04_member_ready() THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::uuid; RETURN;
  END IF;
  SELECT a.* INTO existing FROM company.assistants a
    WHERE a.org_id::text = current_setting('company.org_id', true)
      AND a.owner_id::text = current_setting('company.member_id', true)
      AND a.state = 'active' FOR UPDATE;
  IF FOUND THEN
    RETURN QUERY SELECT 'existing'::text, existing.id, existing.knowledge_namespace; RETURN;
  END IF;
  BEGIN
    INSERT INTO company.assistants(org_id, owner_id)
      VALUES (current_setting('company.org_id', true)::uuid,
              current_setting('company.member_id', true)::uuid)
      RETURNING * INTO existing;
  EXCEPTION WHEN unique_violation THEN
    SELECT a.* INTO existing FROM company.assistants a
      WHERE a.org_id::text = current_setting('company.org_id', true)
        AND a.owner_id::text = current_setting('company.member_id', true)
        AND a.state = 'active';
    IF NOT FOUND THEN RAISE; END IF;
    RETURN QUERY SELECT 'existing'::text, existing.id, existing.knowledge_namespace; RETURN;
  END;
  RETURN QUERY SELECT 'created'::text, existing.id, existing.knowledge_namespace;
END;
$$;

CREATE FUNCTION company.list_personal_assistants()
RETURNS TABLE(assistant_id uuid, knowledge_namespace uuid)
LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT a.id, a.knowledge_namespace FROM company.assistants a
    WHERE company.c04_member_ready() AND a.state = 'active'
    ORDER BY a.id;
$$;

CREATE FUNCTION company.assistant_execution_decision(p_assistant uuid) RETURNS text
LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT CASE WHEN company.c04_member_ready() AND EXISTS (
    SELECT 1 FROM company.assistants a WHERE a.id = p_assistant AND a.state = 'active'
  ) THEN 'allow' ELSE 'denied' END;
$$;

CREATE FUNCTION company.revoke_personal_assistant(p_assistant uuid) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
BEGIN
  IF NOT company.c04_member_ready() THEN RETURN 'denied'; END IF;
  UPDATE company.assistants SET state = 'revoked'
    WHERE id = p_assistant AND state = 'active';
  IF FOUND THEN RETURN 'revoked'; END IF;
  RETURN 'denied';
END;
$$;

CREATE FUNCTION company.put_assistant_knowledge(p_assistant uuid, p_body text)
RETURNS TABLE(decision text, item_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE a company.assistants%ROWTYPE;
DECLARE new_id uuid;
BEGIN
  SELECT * INTO a FROM company.assistants WHERE id = p_assistant AND state = 'active' FOR SHARE;
  IF NOT FOUND OR NOT company.c04_member_ready() THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  INSERT INTO company.assistant_knowledge(org_id, assistant_id, owner_id, namespace, body)
    VALUES (a.org_id, a.id, a.owner_id, a.knowledge_namespace, p_body) RETURNING id INTO new_id;
  RETURN QUERY SELECT 'created'::text, new_id;
END;
$$;
CREATE FUNCTION company.get_assistant_knowledge(p_assistant uuid, p_item uuid)
RETURNS TABLE(decision text, body text)
LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
  IF company.assistant_execution_decision(p_assistant) = 'allow' THEN
    RETURN QUERY SELECT 'found'::text, k.body FROM company.assistant_knowledge k
      WHERE k.assistant_id = p_assistant AND k.id = p_item;
    IF FOUND THEN RETURN; END IF;
  END IF;
  RETURN QUERY SELECT 'denied'::text, NULL::text;
END;
$$;

CREATE FUNCTION company.record_assistant_output(p_assistant uuid, p_body text)
RETURNS TABLE(decision text, output_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE a company.assistants%ROWTYPE;
DECLARE new_id uuid;
BEGIN
  SELECT * INTO a FROM company.assistants WHERE id = p_assistant AND state = 'active' FOR SHARE;
  IF NOT FOUND OR NOT company.c04_member_ready() THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  INSERT INTO company.assistant_outputs(org_id, assistant_id, owner_id, namespace, body)
    VALUES (a.org_id, a.id, a.owner_id, a.knowledge_namespace, p_body) RETURNING id INTO new_id;
  RETURN QUERY SELECT 'created'::text, new_id;
END;
$$;
CREATE FUNCTION company.get_assistant_output(p_assistant uuid, p_output uuid)
RETURNS TABLE(decision text, body text)
LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
  IF company.assistant_execution_decision(p_assistant) = 'allow' THEN
    RETURN QUERY SELECT 'found'::text, o.body FROM company.assistant_outputs o
      WHERE o.assistant_id = p_assistant AND o.id = p_output;
    IF FOUND THEN RETURN; END IF;
  END IF;
  RETURN QUERY SELECT 'denied'::text, NULL::text;
END;
$$;
