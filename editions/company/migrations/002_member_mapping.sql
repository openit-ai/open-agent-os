-- C03 mapping operations run as the caller and retain C01 FORCE RLS.
-- The trusted server sets company.org_id/member_id after authenticating the actor.
CREATE FUNCTION company.c03_admin_ready() RETURNS boolean
LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT EXISTS (
    SELECT 1 FROM company.members m
    JOIN company.organizations o ON o.id = m.org_id
    WHERE m.org_id::text = current_setting('company.org_id', true)
      AND m.id::text = current_setting('company.member_id', true)
      AND m.role = 'admin' AND m.state = 'active' AND o.state = 'active'
  );
$$;

CREATE POLICY external_account_admin_insert ON company.external_accounts FOR INSERT
  WITH CHECK (org_id::text = current_setting('company.org_id', true)
              AND company.c03_admin_ready());

CREATE FUNCTION company.resolve_project_member(p_project_user_id text)
RETURNS TABLE(decision text, mapped_member_id uuid, mapped_role text)
LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
DECLARE found_member company.members%ROWTYPE;
BEGIN
  IF NOT company.c03_admin_ready() THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::text;
    RETURN;
  END IF;
  SELECT m.* INTO found_member FROM company.members m
    WHERE m.org_id::text = current_setting('company.org_id', true)
      AND m.project_user_id = p_project_user_id
    ORDER BY (m.state <> 'departed') DESC, m.created_at DESC LIMIT 1;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'unregistered'::text, NULL::uuid, NULL::text;
  ELSIF found_member.state = 'departed' THEN
    RETURN QUERY SELECT 'departed'::text, NULL::uuid, NULL::text;
  ELSIF found_member.state <> 'active' THEN
    RETURN QUERY SELECT 'inactive'::text, NULL::uuid, NULL::text;
  ELSE
    RETURN QUERY SELECT 'allow'::text, found_member.id, found_member.role;
  END IF;
END;
$$;

CREATE FUNCTION company.resolve_external_member(p_provider text, p_workspace_id text, p_subject_id text)
RETURNS TABLE(decision text, mapped_member_id uuid, mapped_role text)
LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
DECLARE found_account company.external_accounts%ROWTYPE;
DECLARE found_member company.members%ROWTYPE;
BEGIN
  IF NOT company.c03_admin_ready() THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::text;
    RETURN;
  END IF;
  SELECT a.* INTO found_account FROM company.external_accounts a
    WHERE a.org_id::text = current_setting('company.org_id', true)
      AND a.provider = p_provider AND a.workspace_id = p_workspace_id
      AND a.subject_id = p_subject_id
    ORDER BY (a.state <> 'revoked') DESC LIMIT 1;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'unregistered'::text, NULL::uuid, NULL::text;
    RETURN;
  END IF;
  SELECT m.* INTO found_member FROM company.members m
    WHERE m.org_id = found_account.org_id AND m.id = found_account.member_id;
  IF found_member.state = 'departed' THEN
    RETURN QUERY SELECT 'departed'::text, NULL::uuid, NULL::text;
  ELSIF found_account.state <> 'active' THEN
    RETURN QUERY SELECT 'unregistered'::text, NULL::uuid, NULL::text;
  ELSIF found_member.state <> 'active' THEN
    RETURN QUERY SELECT 'inactive'::text, NULL::uuid, NULL::text;
  ELSE
    RETURN QUERY SELECT 'allow'::text, found_member.id, found_member.role;
  END IF;
END;
$$;

CREATE FUNCTION company.bind_external_account(p_member_id uuid, p_provider text,
  p_workspace_id text, p_subject_id text)
RETURNS TABLE(decision text, account_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE owner_state text;
DECLARE new_id uuid;
BEGIN
  IF NOT company.c03_admin_ready() THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid;
    RETURN;
  END IF;
  SELECT m.state INTO owner_state FROM company.members m
    WHERE m.org_id::text = current_setting('company.org_id', true) AND m.id = p_member_id
    FOR SHARE;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'unregistered'::text, NULL::uuid;
    RETURN;
  ELSIF owner_state <> 'active' THEN
    RETURN QUERY SELECT 'inactive'::text, NULL::uuid;
    RETURN;
  END IF;
  BEGIN
    INSERT INTO company.external_accounts(org_id, member_id, provider, workspace_id, subject_id, state)
      VALUES (current_setting('company.org_id', true)::uuid, p_member_id,
              p_provider, p_workspace_id, p_subject_id, 'active')
      RETURNING id INTO new_id;
  EXCEPTION WHEN unique_violation THEN
    RETURN QUERY SELECT 'duplicate'::text, NULL::uuid;
    RETURN;
  END;
  RETURN QUERY SELECT 'created'::text, new_id;
END;
$$;
