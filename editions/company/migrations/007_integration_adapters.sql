-- C11-C15 optional integration adapters. An organization activates only the
-- services it already uses: Google Workspace or Microsoft 365 (G11), Slack
-- and/or Notion (G12), each selected or SKIP with no new subscription.
--
-- No organization app secret or personal OAuth token is stored here. Organization
-- app credentials live in the Company EnvironmentFile (0600). Per-member delegated
-- tokens live in the C05 Secret Vault as an AES-256-GCM envelope bound to
-- org_id/owner_id/provider/purpose; this schema stores only the secret reference.
--
-- The trusted server sets company.org_id and company.member_id after
-- authenticating the Project session. External input never supplies the owner.

CREATE TABLE company.integration_settings (
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  provider text NOT NULL CHECK (provider IN
    ('slack', 'notion', 'google_workspace', 'microsoft_365')),
  state text NOT NULL CHECK (state IN ('skip', 'selected')),
  reason text,
  selected_by uuid,
  selected_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, provider),
  FOREIGN KEY (org_id, selected_by) REFERENCES company.members(org_id, id)
);

CREATE TABLE company.integration_apps (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  provider text NOT NULL CHECK (provider IN
    ('slack', 'notion', 'google_workspace', 'microsoft_365')),
  workspace_id text NOT NULL CHECK (workspace_id <> ''),
  tenant_id text,
  redirect_uri text,
  scopes text[] NOT NULL DEFAULT '{}'::text[],
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'revoked')),
  configured_by uuid,
  configured_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, configured_by) REFERENCES company.members(org_id, id),
  UNIQUE (org_id, id),
  CHECK (provider <> 'microsoft_365' OR tenant_id IS NOT NULL),
  CHECK (provider NOT IN ('google_workspace', 'microsoft_365')
         OR redirect_uri IS NOT NULL)
);
CREATE UNIQUE INDEX integration_apps_live
  ON company.integration_apps(org_id, provider, workspace_id) WHERE state = 'active';

-- Per-member delegated connection for the productivity suites only. Slack and
-- Notion use the organization app plus subject mapping, not a member token.
-- The delegation envelope must reference the composite secret identity.
ALTER TABLE company.secrets ADD CONSTRAINT secrets_org_id_key UNIQUE (org_id, id);
CREATE TABLE company.integration_delegations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  member_id uuid NOT NULL,
  provider text NOT NULL CHECK (provider IN ('google_workspace', 'microsoft_365')),
  subject_id text NOT NULL CHECK (subject_id <> ''),
  workspace_id text NOT NULL CHECK (workspace_id <> ''),
  scopes text[] NOT NULL CHECK (array_length(scopes, 1) >= 1),
  secret_id uuid NOT NULL,
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'revoked')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, member_id) REFERENCES company.members(org_id, id),
  FOREIGN KEY (org_id, secret_id) REFERENCES company.secrets(org_id, id),
  UNIQUE (org_id, id)
);
CREATE UNIQUE INDEX integration_delegations_live
  ON company.integration_delegations(org_id, provider, subject_id) WHERE state = 'active';

-- Only explicit read-only scopes are accepted. Write scopes and application-wide
-- mailbox/file permissions (Mail.ReadWrite, *.All, etc.) are never in this list.
CREATE FUNCTION company.integration_scope_allowed(p_provider text, p_scope text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE p_provider
    WHEN 'google_workspace' THEN p_scope IN (
      'https://www.googleapis.com/auth/gmail.readonly',
      'https://www.googleapis.com/auth/drive.readonly',
      'https://www.googleapis.com/auth/calendar.readonly',
      'https://www.googleapis.com/auth/documents.readonly')
    WHEN 'microsoft_365' THEN p_scope IN (
      'Mail.Read', 'Files.Read', 'Calendars.Read', 'User.Read',
      'offline_access', 'openid', 'profile', 'email')
    ELSE false
  END;
$$;

CREATE FUNCTION company.integration_app_ready(p_org uuid, p_provider text)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT EXISTS (SELECT 1 FROM company.integration_apps a
    WHERE a.org_id = p_org AND a.provider = p_provider AND a.state = 'active');
$$;

-- Selection is an administrator decision. Selecting one productivity suite makes
-- the sibling suite SKIP; a conflicting selection is rejected. Turning a provider
-- off (skip) revokes its active app and all of its delegations.
CREATE FUNCTION company.set_integration_state(p_provider text, p_state text,
  p_reason text) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
DECLARE member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
DECLARE sibling text;
DECLARE sibling_state text;
BEGIN
  IF NOT company.c03_admin_ready() OR org IS NULL
     OR p_provider NOT IN ('slack', 'notion', 'google_workspace', 'microsoft_365')
     OR p_state NOT IN ('skip', 'selected') THEN
    RETURN 'denied';
  END IF;
  IF p_provider IN ('google_workspace', 'microsoft_365') AND p_state = 'selected' THEN
    sibling := CASE p_provider WHEN 'google_workspace' THEN 'microsoft_365'
                               ELSE 'google_workspace' END;
    SELECT s.state INTO sibling_state FROM company.integration_settings s
      WHERE s.org_id = org AND s.provider = sibling;
    IF COALESCE(sibling_state, 'skip') = 'selected' THEN
      RETURN 'conflict';
    END IF;
  END IF;
  INSERT INTO company.integration_settings(org_id, provider, state, reason, selected_by)
  VALUES (org, p_provider, p_state, p_reason, member)
  ON CONFLICT (org_id, provider) DO UPDATE
    SET state = EXCLUDED.state, reason = EXCLUDED.reason,
        selected_by = EXCLUDED.selected_by, selected_at = now();
  IF p_state = 'skip' THEN
    UPDATE company.integration_delegations d SET state = 'revoked', updated_at = now()
      WHERE d.org_id = org AND d.provider = p_provider AND d.state = 'active';
    UPDATE company.integration_apps a SET state = 'revoked', updated_at = now()
      WHERE a.org_id = org AND a.provider = p_provider AND a.state = 'active';
  END IF;
  RETURN 'configured';
END;
$$;

CREATE FUNCTION company.integration_selection(p_provider text)
RETURNS text LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT CASE
    WHEN NOT company.c04_member_ready() THEN 'denied'
    ELSE COALESCE((SELECT s.state FROM company.integration_settings s
      WHERE s.org_id::text = current_setting('company.org_id', true)
        AND s.provider = p_provider), 'skip')
  END;
$$;

CREATE FUNCTION company.configure_integration_app(p_provider text,
  p_workspace_id text, p_tenant_id text, p_redirect_uri text, p_scopes text[])
RETURNS TABLE(decision text, app_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
DECLARE member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
DECLARE scope text;
DECLARE existing company.integration_apps%ROWTYPE;
DECLARE new_app company.integration_apps%ROWTYPE;
BEGIN
  IF NOT company.c03_admin_ready() OR org IS NULL
     OR p_provider NOT IN ('slack', 'notion', 'google_workspace', 'microsoft_365')
     OR p_workspace_id IS NULL OR p_workspace_id = '' THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  IF COALESCE((SELECT s.state FROM company.integration_settings s
        WHERE s.org_id = org AND s.provider = p_provider), 'skip') <> 'selected' THEN
    RETURN QUERY SELECT 'not_selected'::text, NULL::uuid; RETURN;
  END IF;
  IF p_provider = 'microsoft_365' AND (p_tenant_id IS NULL OR p_tenant_id = '') THEN
    RETURN QUERY SELECT 'invalid'::text, NULL::uuid; RETURN;
  END IF;
  IF p_provider IN ('google_workspace', 'microsoft_365') THEN
    IF p_redirect_uri IS NULL OR p_redirect_uri !~ '^https://[A-Za-z0-9./_-]+$'
       OR p_scopes IS NULL OR array_length(p_scopes, 1) IS NULL THEN
      RETURN QUERY SELECT 'invalid'::text, NULL::uuid; RETURN;
    END IF;
    FOREACH scope IN ARRAY p_scopes LOOP
      IF NOT company.integration_scope_allowed(p_provider, scope) THEN
        RETURN QUERY SELECT 'invalid_scope'::text, NULL::uuid; RETURN;
      END IF;
    END LOOP;
  END IF;
  SELECT a.* INTO existing FROM company.integration_apps a
    WHERE a.org_id = org AND a.provider = p_provider AND a.state = 'active'
    FOR UPDATE;
  IF FOUND THEN
    UPDATE company.integration_apps a
      SET workspace_id = p_workspace_id, tenant_id = p_tenant_id,
          redirect_uri = p_redirect_uri, scopes = COALESCE(p_scopes, '{}'::text[]),
          configured_by = member, updated_at = now()
      WHERE a.id = existing.id RETURNING * INTO new_app;
  ELSE
    INSERT INTO company.integration_apps(org_id, provider, workspace_id, tenant_id,
      redirect_uri, scopes, configured_by)
    VALUES (org, p_provider, p_workspace_id, p_tenant_id, p_redirect_uri,
      COALESCE(p_scopes, '{}'::text[]), member)
    RETURNING * INTO new_app;
  END IF;
  RETURN QUERY SELECT 'configured'::text, new_app.id;
END;
$$;

CREATE FUNCTION company.revoke_integration_app(p_provider text) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
DECLARE affected integer;
BEGIN
  IF NOT company.c03_admin_ready() OR org IS NULL THEN RETURN 'denied'; END IF;
  UPDATE company.integration_delegations d SET state = 'revoked', updated_at = now()
    WHERE d.org_id = org AND d.provider = p_provider AND d.state = 'active';
  UPDATE company.integration_apps a SET state = 'revoked', updated_at = now()
    WHERE a.org_id = org AND a.provider = p_provider AND a.state = 'active';
  GET DIAGNOSTICS affected = ROW_COUNT;
  IF affected > 0 THEN RETURN 'revoked'; END IF;
  RETURN 'denied';
END;
$$;

-- A member binds only their own delegated account. The referenced secret must be
-- that member's active C05 envelope for the same provider and purpose.
CREATE FUNCTION company.bind_delegation(p_provider text, p_subject_id text,
  p_workspace_id text, p_scopes text[], p_secret_id uuid)
RETURNS TABLE(decision text, delegation_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
DECLARE member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
DECLARE scope text;
DECLARE secret_ok boolean;
DECLARE new_del company.integration_delegations%ROWTYPE;
BEGIN
  IF NOT company.c04_member_ready() OR org IS NULL OR member IS NULL
     OR p_provider NOT IN ('google_workspace', 'microsoft_365')
     OR p_subject_id IS NULL OR p_subject_id = ''
     OR p_workspace_id IS NULL OR p_workspace_id = ''
     OR p_scopes IS NULL OR array_length(p_scopes, 1) IS NULL THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  IF COALESCE((SELECT s.state FROM company.integration_settings s
        WHERE s.org_id = org AND s.provider = p_provider), 'skip') <> 'selected'
     OR NOT company.integration_app_ready(org, p_provider) THEN
    RETURN QUERY SELECT 'not_selected'::text, NULL::uuid; RETURN;
  END IF;
  FOREACH scope IN ARRAY p_scopes LOOP
    IF NOT company.integration_scope_allowed(p_provider, scope) THEN
      RETURN QUERY SELECT 'invalid_scope'::text, NULL::uuid; RETURN;
    END IF;
  END LOOP;
  SELECT EXISTS (SELECT 1 FROM company.secrets s
    WHERE s.id = p_secret_id AND s.org_id = org AND s.owner_id = member
      AND s.provider = p_provider AND s.purpose = 'oauth_delegation'
      AND s.state = 'active') INTO secret_ok;
  IF NOT secret_ok THEN
    RETURN QUERY SELECT 'secret_unavailable'::text, NULL::uuid; RETURN;
  END IF;
  BEGIN
    INSERT INTO company.integration_delegations(org_id, member_id, provider,
      subject_id, workspace_id, scopes, secret_id)
    VALUES (org, member, p_provider, p_subject_id, p_workspace_id, p_scopes, p_secret_id)
    RETURNING * INTO new_del;
  EXCEPTION WHEN unique_violation THEN
    RETURN QUERY SELECT 'duplicate'::text, NULL::uuid; RETURN;
  END;
  RETURN QUERY SELECT 'created'::text, new_del.id;
END;
$$;

CREATE FUNCTION company.delegation_use_decision(p_delegation uuid)
RETURNS TABLE(decision text, reason_code text)
LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
DECLARE del company.integration_delegations%ROWTYPE;
DECLARE secret_active boolean;
BEGIN
  IF NOT company.c04_member_ready() THEN
    RETURN QUERY SELECT 'denied'::text, 'member_inactive'::text; RETURN;
  END IF;
  SELECT d.* INTO del FROM company.integration_delegations d
    WHERE d.id = p_delegation
      AND d.org_id::text = current_setting('company.org_id', true)
      AND d.member_id::text = current_setting('company.member_id', true);
  IF NOT FOUND OR del.state <> 'active' THEN
    RETURN QUERY SELECT 'denied'::text, 'delegation_unavailable'::text; RETURN;
  END IF;
  IF COALESCE((SELECT s.state FROM company.integration_settings s
        WHERE s.org_id = del.org_id AND s.provider = del.provider), 'skip') <> 'selected' THEN
    RETURN QUERY SELECT 'denied'::text, 'not_selected'::text; RETURN;
  END IF;
  IF NOT company.integration_app_ready(del.org_id, del.provider) THEN
    RETURN QUERY SELECT 'denied'::text, 'app_missing'::text; RETURN;
  END IF;
  SELECT EXISTS (SELECT 1 FROM company.secrets s
    WHERE s.id = del.secret_id AND s.state = 'active') INTO secret_active;
  IF NOT secret_active THEN
    RETURN QUERY SELECT 'denied'::text, 'secret_unavailable'::text; RETURN;
  END IF;
  RETURN QUERY SELECT 'allow'::text, 'delegation_active'::text;
END;
$$;

CREATE FUNCTION company.revoke_delegation(p_delegation uuid) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE del company.integration_delegations%ROWTYPE;
BEGIN
  IF NOT company.c04_member_ready() THEN RETURN 'denied'; END IF;
  SELECT d.* INTO del FROM company.integration_delegations d
    WHERE d.id = p_delegation AND d.state = 'active'
      AND d.member_id::text = current_setting('company.member_id', true)
    FOR UPDATE;
  IF NOT FOUND THEN RETURN 'denied'; END IF;
  UPDATE company.integration_delegations d SET state = 'revoked', updated_at = now()
    WHERE d.id = del.id;
  -- Revoking the owner's own vault credential is permitted by the C05 policy.
  UPDATE company.secrets s SET state = 'revoked'
    WHERE s.id = del.secret_id AND s.owner_id = del.member_id AND s.state = 'active';
  RETURN 'revoked';
END;
$$;

-- Slack event routing. The trusted server calls this after verifying the request
-- signature and maps the sender to a member and that member's active assistant.
CREATE FUNCTION company.slack_route_decision(p_workspace_id text, p_subject_id text)
RETURNS TABLE(decision text, reason_code text, member_id uuid, assistant_id uuid)
LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
DECLARE org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
DECLARE account company.external_accounts%ROWTYPE;
DECLARE found_member company.members%ROWTYPE;
DECLARE found_assistant company.assistants%ROWTYPE;
BEGIN
  IF NOT company.c04_member_ready() OR org IS NULL THEN
    RETURN QUERY SELECT 'denied'::text, 'member_inactive'::text, NULL::uuid, NULL::uuid; RETURN;
  END IF;
  IF COALESCE((SELECT s.state FROM company.integration_settings s
        WHERE s.org_id = org AND s.provider = 'slack'), 'skip') <> 'selected' THEN
    RETURN QUERY SELECT 'denied'::text, 'not_selected'::text, NULL::uuid, NULL::uuid; RETURN;
  END IF;
  IF NOT company.integration_app_ready(org, 'slack') THEN
    RETURN QUERY SELECT 'denied'::text, 'app_missing'::text, NULL::uuid, NULL::uuid; RETURN;
  END IF;
  SELECT a.* INTO account FROM company.external_accounts a
    WHERE a.org_id = org AND a.provider = 'slack'
      AND a.workspace_id = p_workspace_id AND a.subject_id = p_subject_id
      AND a.state = 'active';
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'denied'::text, 'unregistered'::text, NULL::uuid, NULL::uuid; RETURN;
  END IF;
  SELECT m.* INTO found_member FROM company.members m
    WHERE m.org_id = org AND m.id = account.member_id;
  IF found_member.state <> 'active' THEN
    RETURN QUERY SELECT 'denied'::text, 'inactive'::text, NULL::uuid, NULL::uuid; RETURN;
  END IF;
  SELECT a.* INTO found_assistant FROM company.assistants a
    WHERE a.org_id = org AND a.owner_id = found_member.id AND a.state = 'active';
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'denied'::text, 'no_assistant'::text, NULL::uuid, NULL::uuid; RETURN;
  END IF;
  RETURN QUERY SELECT 'allow'::text, 'routed'::text, found_member.id, found_assistant.id;
END;
$$;

-- Notion synchronization keeps the original ACL version on the document. A
-- candidate is only readable while the synced ACL version is present and the
-- document is not deleted; otherwise the search layer must not build snippets.
CREATE FUNCTION company.sync_notion_document(p_workspace_id text, p_source_id text,
  p_acl_version text, p_body text)
RETURNS TABLE(decision text, document_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
DECLARE doc company.documents%ROWTYPE;
BEGIN
  IF NOT company.c03_admin_ready() OR org IS NULL
     OR p_source_id IS NULL OR p_source_id = ''
     OR p_acl_version IS NULL OR p_acl_version = '' THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  IF COALESCE((SELECT s.state FROM company.integration_settings s
        WHERE s.org_id = org AND s.provider = 'notion'), 'skip') <> 'selected'
     OR NOT company.integration_app_ready(org, 'notion') THEN
    RETURN QUERY SELECT 'not_selected'::text, NULL::uuid; RETURN;
  END IF;
  INSERT INTO company.documents(org_id, source, source_id, owner_id, source_acl_version)
  VALUES (org, 'notion', p_source_id, NULL, p_acl_version)
  ON CONFLICT (org_id, source, source_id) DO UPDATE
    SET source_acl_version = EXCLUDED.source_acl_version,
        deleted_at = NULL, updated_at = now()
  RETURNING * INTO doc;
  DELETE FROM company.chunks c WHERE c.document_id = doc.id;
  IF p_body IS NOT NULL AND p_body <> '' THEN
    INSERT INTO company.chunks(org_id, document_id, ordinal, body)
      VALUES (org, doc.id, 0, p_body);
  END IF;
  RETURN QUERY SELECT 'synced'::text, doc.id;
END;
$$;

CREATE FUNCTION company.remove_notion_document(p_source_id text) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
DECLARE doc_id uuid;
BEGIN
  IF NOT company.c03_admin_ready() OR org IS NULL THEN RETURN 'denied'; END IF;
  UPDATE company.documents d SET deleted_at = now(), updated_at = now()
    WHERE d.org_id = org AND d.source = 'notion' AND d.source_id = p_source_id
      AND d.deleted_at IS NULL
    RETURNING d.id INTO doc_id;
  IF NOT FOUND THEN RETURN 'denied'; END IF;
  DELETE FROM company.chunks c WHERE c.document_id = doc_id;
  RETURN 'removed';
END;
$$;

CREATE FUNCTION company.notion_document_decision(p_document uuid)
RETURNS TABLE(decision text, reason_code text)
LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
DECLARE org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
DECLARE doc company.documents%ROWTYPE;
BEGIN
  IF NOT company.c04_member_ready() OR org IS NULL THEN
    RETURN QUERY SELECT 'denied'::text, 'member_inactive'::text; RETURN;
  END IF;
  IF COALESCE((SELECT s.state FROM company.integration_settings s
        WHERE s.org_id = org AND s.provider = 'notion'), 'skip') <> 'selected' THEN
    RETURN QUERY SELECT 'denied'::text, 'not_selected'::text; RETURN;
  END IF;
  IF NOT company.integration_app_ready(org, 'notion') THEN
    RETURN QUERY SELECT 'denied'::text, 'app_missing'::text; RETURN;
  END IF;
  SELECT d.* INTO doc FROM company.documents d
    WHERE d.id = p_document AND d.org_id = org AND d.source = 'notion';
  IF NOT FOUND OR doc.deleted_at IS NOT NULL OR doc.source_acl_version = '' THEN
    RETURN QUERY SELECT 'denied'::text, 'acl_unconfirmed'::text; RETURN;
  END IF;
  RETURN QUERY SELECT 'allow'::text, 'acl_synced'::text;
END;
$$;

-- Bindings are immutable; revoke and create a new row instead of rebinding.
CREATE FUNCTION company.integration_binding_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.org_id IS DISTINCT FROM OLD.org_id THEN
    RAISE EXCEPTION 'integration identity is immutable';
  END IF;
  IF TG_TABLE_NAME = 'integration_delegations'
     AND (NEW.member_id IS DISTINCT FROM OLD.member_id
          OR NEW.provider IS DISTINCT FROM OLD.provider
          OR NEW.subject_id IS DISTINCT FROM OLD.subject_id
          OR NEW.secret_id IS DISTINCT FROM OLD.secret_id) THEN
    RAISE EXCEPTION 'delegation binding is immutable; revoke and create a new row';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;
CREATE TRIGGER integration_apps_immutable BEFORE UPDATE OF id, org_id
  ON company.integration_apps FOR EACH ROW
  EXECUTE FUNCTION company.integration_binding_immutable();
CREATE TRIGGER integration_delegations_immutable BEFORE UPDATE OF id, org_id, member_id, provider, subject_id, secret_id
  ON company.integration_delegations FOR EACH ROW
  EXECUTE FUNCTION company.integration_binding_immutable();

-- A delegating secret must belong to the same org/member/provider as the row.
CREATE FUNCTION company.integration_delegation_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE secret_owner uuid;
DECLARE secret_provider text;
DECLARE secret_purpose text;
BEGIN
  SELECT s.owner_id, s.provider, s.purpose INTO secret_owner, secret_provider, secret_purpose
    FROM company.secrets s WHERE s.org_id = NEW.org_id AND s.id = NEW.secret_id;
  IF secret_owner IS DISTINCT FROM NEW.member_id
     OR secret_provider IS DISTINCT FROM NEW.provider
     OR secret_purpose IS DISTINCT FROM 'oauth_delegation' THEN
    RAISE EXCEPTION 'delegation secret must be the owner provider envelope';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER integration_delegations_guard BEFORE INSERT OR UPDATE OF secret_id, member_id, provider
  ON company.integration_delegations FOR EACH ROW
  EXECUTE FUNCTION company.integration_delegation_guard();

-- Selection, app and delegation changes are audited without any credential.
CREATE FUNCTION company.integration_audit() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actor uuid;
DECLARE owner uuid;
DECLARE action_name text;
DECLARE metadata jsonb;
BEGIN
  actor := NULLIF(current_setting('company.member_id', true), '')::uuid;
  owner := NULL;
  IF TG_TABLE_NAME = 'integration_settings' THEN
    action_name := 'integration_' || NEW.state;
    metadata := jsonb_build_object('provider', NEW.provider, 'state', NEW.state);
  ELSIF TG_TABLE_NAME = 'integration_apps' THEN
    action_name := 'app_' || NEW.state;
    metadata := jsonb_build_object('provider', NEW.provider,
      'workspace_id', NEW.workspace_id, 'state', NEW.state,
      'scopes', to_jsonb(NEW.scopes));
  ELSE
    action_name := 'delegation_' || NEW.state;
    owner := NEW.member_id;
    metadata := jsonb_build_object('provider', NEW.provider,
      'subject_id', NEW.subject_id, 'state', NEW.state,
      'scopes', to_jsonb(NEW.scopes));
  END IF;
  INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
    resource_ref, decision, request_id, metadata)
  VALUES (NEW.org_id, actor, owner, 'integration', action_name,
    COALESCE(to_jsonb(NEW)->>'id', NEW.org_id::text), 'allow',
    gen_random_uuid()::text, metadata);
  RETURN NEW;
END;
$$;
CREATE TRIGGER integration_settings_audit AFTER INSERT OR UPDATE
  ON company.integration_settings FOR EACH ROW EXECUTE FUNCTION company.integration_audit();
CREATE TRIGGER integration_apps_audit AFTER INSERT OR UPDATE
  ON company.integration_apps FOR EACH ROW EXECUTE FUNCTION company.integration_audit();
CREATE TRIGGER integration_delegations_audit AFTER INSERT OR UPDATE
  ON company.integration_delegations FOR EACH ROW EXECUTE FUNCTION company.integration_audit();

ALTER TABLE company.integration_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.integration_apps ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.integration_delegations ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.integration_settings FORCE ROW LEVEL SECURITY;
ALTER TABLE company.integration_apps FORCE ROW LEVEL SECURITY;
ALTER TABLE company.integration_delegations FORCE ROW LEVEL SECURITY;

CREATE POLICY integration_settings_read ON company.integration_settings FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true) AND company.c04_member_ready());
CREATE POLICY integration_settings_insert ON company.integration_settings FOR INSERT
  WITH CHECK (org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready());
CREATE POLICY integration_settings_update ON company.integration_settings FOR UPDATE
  USING (org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready())
  WITH CHECK (org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready());
CREATE POLICY integration_apps_read ON company.integration_apps FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true) AND company.c04_member_ready());
CREATE POLICY integration_apps_insert ON company.integration_apps FOR INSERT
  WITH CHECK (org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready());
CREATE POLICY integration_apps_update ON company.integration_apps FOR UPDATE
  USING (org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready())
  WITH CHECK (org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready());
CREATE POLICY integration_delegations_read ON company.integration_delegations FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true)
         AND (company.c03_admin_ready()
              OR member_id::text = current_setting('company.member_id', true)));
CREATE POLICY integration_delegations_insert ON company.integration_delegations FOR INSERT
  WITH CHECK (org_id::text = current_setting('company.org_id', true)
              AND member_id::text = current_setting('company.member_id', true)
              AND company.c04_member_ready());
CREATE POLICY integration_delegations_update ON company.integration_delegations FOR UPDATE
  USING (org_id::text = current_setting('company.org_id', true)
         AND (company.c03_admin_ready()
              OR member_id::text = current_setting('company.member_id', true)))
  WITH CHECK (org_id::text = current_setting('company.org_id', true)
              AND (company.c03_admin_ready()
                   OR member_id::text = current_setting('company.member_id', true)));
