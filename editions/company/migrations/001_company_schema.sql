-- Applied with psql --single-transaction together with its checksum record.
-- The application must set company.org_id and company.member_id from its verified session.
CREATE SCHEMA IF NOT EXISTS company;

CREATE TABLE company.schema_migrations (
  version integer PRIMARY KEY CHECK (version > 0),
  checksum char(64) NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
  applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE company.organizations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug text NOT NULL UNIQUE CHECK (slug <> ''),
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'suspended')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE company.members (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  project_user_id text NOT NULL CHECK (project_user_id <> ''),
  role text NOT NULL CHECK (role IN ('admin', 'member')),
  state text NOT NULL DEFAULT 'invited' CHECK (state IN ('invited', 'active', 'suspended', 'departed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id)
);
CREATE UNIQUE INDEX members_live_project_user ON company.members(org_id, project_user_id)
  WHERE state <> 'departed';

CREATE TABLE company.external_accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  member_id uuid NOT NULL,
  provider text NOT NULL CHECK (provider <> ''),
  workspace_id text NOT NULL CHECK (workspace_id <> ''),
  subject_id text NOT NULL CHECK (subject_id <> ''),
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'active', 'revoked')),
  FOREIGN KEY (org_id, member_id) REFERENCES company.members(org_id, id)
);
CREATE UNIQUE INDEX external_accounts_live_subject ON company.external_accounts(org_id, provider, workspace_id, subject_id)
  WHERE state <> 'revoked';

CREATE TABLE company.assistants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  owner_id uuid NOT NULL,
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'suspended', 'revoked')),
  knowledge_namespace uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  FOREIGN KEY (org_id, owner_id) REFERENCES company.members(org_id, id),
  UNIQUE (org_id, id)
);
CREATE UNIQUE INDEX assistants_live_owner ON company.assistants(org_id, owner_id)
  WHERE state <> 'revoked';

CREATE TABLE company.secrets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  owner_id uuid,
  provider text NOT NULL CHECK (provider <> ''),
  purpose text NOT NULL CHECK (purpose <> ''),
  ciphertext bytea NOT NULL CHECK (length(ciphertext) > 0),
  nonce bytea NOT NULL CHECK (length(nonce) > 0),
  key_version integer NOT NULL CHECK (key_version > 0),
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'revoked')),
  FOREIGN KEY (org_id, owner_id) REFERENCES company.members(org_id, id)
);
CREATE UNIQUE INDEX secrets_active_organization ON company.secrets(org_id, provider, purpose)
  WHERE owner_id IS NULL AND state = 'active';
CREATE UNIQUE INDEX secrets_active_personal ON company.secrets(org_id, owner_id, provider, purpose)
  WHERE owner_id IS NOT NULL AND state = 'active';

CREATE TABLE company.policies (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  action text NOT NULL CHECK (action <> ''),
  risk text NOT NULL CHECK (risk <> ''),
  decision text NOT NULL CHECK (decision IN ('deny', 'allow', 'approval')),
  version integer NOT NULL CHECK (version > 0),
  UNIQUE (org_id, action)
);

CREATE TABLE company.approvals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  requester_id uuid NOT NULL,
  approver_id uuid,
  action text NOT NULL CHECK (action <> ''),
  resource_digest text NOT NULL CHECK (resource_digest <> ''),
  context_digest text NOT NULL CHECK (context_digest <> ''),
  policy_version integer NOT NULL CHECK (policy_version > 0),
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'approved', 'denied', 'expired', 'consumed')),
  FOREIGN KEY (org_id, requester_id) REFERENCES company.members(org_id, id),
  FOREIGN KEY (org_id, approver_id) REFERENCES company.members(org_id, id),
  CHECK (approver_id IS NULL OR requester_id <> approver_id),
  CHECK (state <> 'consumed' OR consumed_at IS NOT NULL)
);

CREATE TABLE company.audit_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  at timestamptz NOT NULL DEFAULT now(),
  actor_id uuid,
  owner_id uuid,
  event_type text NOT NULL CHECK (event_type <> ''),
  action text NOT NULL CHECK (action <> ''),
  resource_ref text,
  decision text NOT NULL CHECK (decision <> ''),
  reason_code text,
  request_id text NOT NULL CHECK (request_id <> ''),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  FOREIGN KEY (org_id, actor_id) REFERENCES company.members(org_id, id),
  FOREIGN KEY (org_id, owner_id) REFERENCES company.members(org_id, id)
);

CREATE TABLE company.documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  source text NOT NULL CHECK (source <> ''),
  source_id text NOT NULL CHECK (source_id <> ''),
  owner_id uuid,
  source_acl_version text NOT NULL CHECK (source_acl_version <> ''),
  deleted_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, owner_id) REFERENCES company.members(org_id, id),
  UNIQUE (org_id, source, source_id),
  UNIQUE (org_id, id),
  CHECK (source <> 'personal' OR owner_id IS NOT NULL)
);

CREATE TABLE company.chunks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL,
  document_id uuid NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  body text NOT NULL,
  FOREIGN KEY (org_id, document_id) REFERENCES company.documents(org_id, id) ON DELETE CASCADE,
  UNIQUE (document_id, ordinal)
);

CREATE FUNCTION company.reject_owner_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME = 'assistants' AND NEW.owner_id IS DISTINCT FROM OLD.owner_id THEN
    RAISE EXCEPTION 'assistant owner is immutable';
  END IF;
  IF TG_TABLE_NAME IN ('secrets', 'documents') AND OLD.owner_id IS NOT NULL
     AND NEW.owner_id IS DISTINCT FROM OLD.owner_id THEN
    RAISE EXCEPTION 'personal owner is immutable';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER assistants_owner_immutable BEFORE UPDATE OF owner_id ON company.assistants
  FOR EACH ROW EXECUTE FUNCTION company.reject_owner_change();
CREATE TRIGGER secrets_owner_immutable BEFORE UPDATE OF owner_id ON company.secrets
  FOR EACH ROW EXECUTE FUNCTION company.reject_owner_change();
CREATE TRIGGER documents_owner_immutable BEFORE UPDATE OF owner_id ON company.documents
  FOR EACH ROW EXECUTE FUNCTION company.reject_owner_change();

CREATE FUNCTION company.reject_identity_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.id IS DISTINCT FROM OLD.id OR
     to_jsonb(NEW)->>'org_id' IS DISTINCT FROM to_jsonb(OLD)->>'org_id' THEN
    RAISE EXCEPTION 'internal identity is immutable';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER organizations_identity_immutable BEFORE UPDATE OF id ON company.organizations
  FOR EACH ROW EXECUTE FUNCTION company.reject_identity_change();
CREATE TRIGGER members_identity_immutable BEFORE UPDATE OF id, org_id ON company.members
  FOR EACH ROW EXECUTE FUNCTION company.reject_identity_change();
CREATE TRIGGER external_accounts_identity_immutable BEFORE UPDATE OF id, org_id ON company.external_accounts
  FOR EACH ROW EXECUTE FUNCTION company.reject_identity_change();
CREATE TRIGGER assistants_identity_immutable BEFORE UPDATE OF id, org_id ON company.assistants
  FOR EACH ROW EXECUTE FUNCTION company.reject_identity_change();
CREATE TRIGGER secrets_identity_immutable BEFORE UPDATE OF id, org_id ON company.secrets
  FOR EACH ROW EXECUTE FUNCTION company.reject_identity_change();
CREATE TRIGGER policies_identity_immutable BEFORE UPDATE OF id, org_id ON company.policies
  FOR EACH ROW EXECUTE FUNCTION company.reject_identity_change();
CREATE TRIGGER approvals_identity_immutable BEFORE UPDATE OF id, org_id ON company.approvals
  FOR EACH ROW EXECUTE FUNCTION company.reject_identity_change();
CREATE TRIGGER audit_events_identity_immutable BEFORE UPDATE OF id, org_id ON company.audit_events
  FOR EACH ROW EXECUTE FUNCTION company.reject_identity_change();
CREATE TRIGGER documents_identity_immutable BEFORE UPDATE OF id, org_id ON company.documents
  FOR EACH ROW EXECUTE FUNCTION company.reject_identity_change();
CREATE TRIGGER chunks_identity_immutable BEFORE UPDATE OF id, org_id ON company.chunks
  FOR EACH ROW EXECUTE FUNCTION company.reject_identity_change();

CREATE FUNCTION company.reject_external_rebinding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.member_id IS DISTINCT FROM OLD.member_id OR
     NEW.provider IS DISTINCT FROM OLD.provider OR
     NEW.workspace_id IS DISTINCT FROM OLD.workspace_id OR
     NEW.subject_id IS DISTINCT FROM OLD.subject_id THEN
    RAISE EXCEPTION 'external account binding is immutable; revoke and create a new row';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER external_accounts_binding_immutable
  BEFORE UPDATE OF member_id, provider, workspace_id, subject_id ON company.external_accounts
  FOR EACH ROW EXECUTE FUNCTION company.reject_external_rebinding();

CREATE FUNCTION company.reject_departed_reactivation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state = 'departed' AND NEW.state <> 'departed' THEN
    RAISE EXCEPTION 'departed member cannot be reactivated';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER members_departed_final BEFORE UPDATE OF state ON company.members
  FOR EACH ROW EXECUTE FUNCTION company.reject_departed_reactivation();

CREATE FUNCTION company.protect_last_admin() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE leaving boolean;
BEGIN
  IF TG_OP = 'DELETE' THEN
    leaving := true;
  ELSE
    leaving := NEW.role <> 'admin' OR NEW.state <> 'active' OR NEW.org_id <> OLD.org_id;
  END IF;
  IF OLD.role = 'admin' AND OLD.state = 'active' AND leaving THEN
    -- Serialize changes to the organization's active administrator set.
    PERFORM 1 FROM company.organizations WHERE id = OLD.org_id FOR UPDATE;
    IF NOT EXISTS (
      SELECT 1 FROM company.members
       WHERE org_id = OLD.org_id AND id <> OLD.id AND role = 'admin' AND state = 'active'
    ) THEN
      RAISE EXCEPTION 'last active administrator cannot leave';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER members_last_admin BEFORE UPDATE OF role, state, org_id OR DELETE ON company.members
  FOR EACH ROW EXECUTE FUNCTION company.protect_last_admin();

CREATE FUNCTION company.revoke_departed_accounts() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state <> 'departed' AND NEW.state = 'departed' THEN
    UPDATE company.external_accounts SET state = 'revoked'
     WHERE org_id = NEW.org_id AND member_id = NEW.id AND state <> 'revoked';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER members_revoke_accounts AFTER UPDATE OF state ON company.members
  FOR EACH ROW EXECUTE FUNCTION company.revoke_departed_accounts();

-- RLS is a defense layer for a trusted application role. The server must set these
-- transaction-local values only after authenticating the Project user.
ALTER TABLE company.organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.members ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.external_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.assistants ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.secrets ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.organizations FORCE ROW LEVEL SECURITY;
ALTER TABLE company.members FORCE ROW LEVEL SECURITY;
ALTER TABLE company.external_accounts FORCE ROW LEVEL SECURITY;
ALTER TABLE company.assistants FORCE ROW LEVEL SECURITY;
ALTER TABLE company.secrets FORCE ROW LEVEL SECURITY;
ALTER TABLE company.policies FORCE ROW LEVEL SECURITY;
ALTER TABLE company.approvals FORCE ROW LEVEL SECURITY;
ALTER TABLE company.audit_events FORCE ROW LEVEL SECURITY;
ALTER TABLE company.documents FORCE ROW LEVEL SECURITY;
ALTER TABLE company.chunks FORCE ROW LEVEL SECURITY;

CREATE POLICY organization_scope ON company.organizations USING (id::text = current_setting('company.org_id', true)) WITH CHECK (id::text = current_setting('company.org_id', true));
CREATE POLICY member_scope ON company.members USING (org_id::text = current_setting('company.org_id', true)) WITH CHECK (org_id::text = current_setting('company.org_id', true));
CREATE POLICY external_account_scope ON company.external_accounts USING (org_id::text = current_setting('company.org_id', true) AND member_id::text = current_setting('company.member_id', true)) WITH CHECK (org_id::text = current_setting('company.org_id', true) AND member_id::text = current_setting('company.member_id', true));
CREATE POLICY external_account_admin_read ON company.external_accounts FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true) AND EXISTS (
    SELECT 1 FROM company.members m WHERE m.org_id = external_accounts.org_id
      AND m.id::text = current_setting('company.member_id', true) AND m.role = 'admin' AND m.state = 'active'
  ));
CREATE POLICY external_account_admin_update ON company.external_accounts FOR UPDATE
  USING (org_id::text = current_setting('company.org_id', true) AND EXISTS (
    SELECT 1 FROM company.members m WHERE m.org_id = external_accounts.org_id
      AND m.id::text = current_setting('company.member_id', true) AND m.role = 'admin' AND m.state = 'active'
  ))
  WITH CHECK (org_id::text = current_setting('company.org_id', true) AND EXISTS (
    SELECT 1 FROM company.members m WHERE m.org_id = external_accounts.org_id
      AND m.id::text = current_setting('company.member_id', true) AND m.role = 'admin' AND m.state = 'active'
  ));
CREATE POLICY assistant_scope ON company.assistants USING (org_id::text = current_setting('company.org_id', true) AND owner_id::text = current_setting('company.member_id', true)) WITH CHECK (org_id::text = current_setting('company.org_id', true) AND owner_id::text = current_setting('company.member_id', true));
CREATE POLICY secret_scope ON company.secrets USING (org_id::text = current_setting('company.org_id', true) AND (owner_id IS NULL OR owner_id::text = current_setting('company.member_id', true))) WITH CHECK (org_id::text = current_setting('company.org_id', true) AND (owner_id IS NULL OR owner_id::text = current_setting('company.member_id', true)));
CREATE POLICY policy_scope ON company.policies USING (org_id::text = current_setting('company.org_id', true)) WITH CHECK (org_id::text = current_setting('company.org_id', true));
CREATE POLICY approval_scope ON company.approvals USING (org_id::text = current_setting('company.org_id', true) AND requester_id::text = current_setting('company.member_id', true)) WITH CHECK (org_id::text = current_setting('company.org_id', true) AND requester_id::text = current_setting('company.member_id', true));
CREATE POLICY audit_scope ON company.audit_events USING (org_id::text = current_setting('company.org_id', true)) WITH CHECK (org_id::text = current_setting('company.org_id', true));
CREATE POLICY document_scope ON company.documents USING (org_id::text = current_setting('company.org_id', true) AND (owner_id IS NULL OR owner_id::text = current_setting('company.member_id', true))) WITH CHECK (org_id::text = current_setting('company.org_id', true) AND (owner_id IS NULL OR owner_id::text = current_setting('company.member_id', true)));
CREATE POLICY chunk_scope ON company.chunks USING (
  org_id::text = current_setting('company.org_id', true) AND EXISTS (
    SELECT 1 FROM company.documents d WHERE d.org_id = chunks.org_id AND d.id = chunks.document_id
  )
) WITH CHECK (
  org_id::text = current_setting('company.org_id', true) AND EXISTS (
    SELECT 1 FROM company.documents d WHERE d.org_id = chunks.org_id AND d.id = chunks.document_id
  )
);
