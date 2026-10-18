-- C11-C14 read-back for one provider. Fixtures and their audit rows are rolled
-- back; the persisted selection is only read for context. No credential is
-- created or printed here beyond a C05 envelope the caller already owns.
BEGIN;
SELECT set_config('oaos.provider', :'provider', true);
DO $verify$
DECLARE
  v_provider text := current_setting('oaos.provider');
  org_a uuid := gen_random_uuid();
  org_b uuid := gen_random_uuid();
  admin_a uuid := gen_random_uuid();
  owner_a uuid := gen_random_uuid();
  owner_b uuid := gen_random_uuid();
  admin_b uuid := gen_random_uuid();
  assistant_a uuid;
  app_a uuid;
  del_a uuid;
  secret_a uuid;
  doc_a uuid;
  decision text;
  reason text;
  member_out uuid;
  assistant_out uuid;
  scope text;
  scopes text[];
  rejected boolean;
BEGIN
  IF v_provider NOT IN ('slack', 'notion', 'google_workspace', 'microsoft_365') THEN
    RAISE EXCEPTION 'unknown provider %', v_provider;
  END IF;

  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_a, 'adapt-a-' || org_a::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (admin_a, org_a, 'adapt-admin-' || admin_a::text, 'admin', 'active'),
    (owner_a, org_a, 'adapt-owner-' || owner_a::text, 'member', 'active'),
    (owner_b, org_a, 'adapt-owner-' || owner_b::text, 'member', 'active');
  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', admin_b::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_b, 'adapt-b-' || org_b::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (admin_b, org_b, 'adapt-admin-' || admin_b::text, 'admin', 'active');
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);

  -- A provider that was never selected stays SKIP and cannot be configured.
  IF company.integration_selection(v_provider) IS DISTINCT FROM 'skip' THEN
    RAISE EXCEPTION 'unselected provider was not skip';
  END IF;
  SELECT r.decision INTO decision FROM company.configure_integration_app(
    v_provider, 'ws-unselected', NULL, NULL, NULL) r;
  IF decision IS DISTINCT FROM 'not_selected' THEN
    RAISE EXCEPTION 'unselected provider app was accepted';
  END IF;

  -- Only an active administrator can change the selection.
  PERFORM set_config('company.member_id', owner_a::text, true);
  IF company.set_integration_state(v_provider, 'selected', 'member_attempt')
     IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'member changed integration selection';
  END IF;
  PERFORM set_config('company.member_id', admin_a::text, true);
  IF company.set_integration_state(v_provider, 'selected', 'operator_selected')
     IS DISTINCT FROM 'configured' THEN
    RAISE EXCEPTION 'administrator selection failed';
  END IF;
  IF company.integration_selection(v_provider) IS DISTINCT FROM 'selected' THEN
    RAISE EXCEPTION 'selection was not persisted';
  END IF;

  -- G11 chooses exactly one productivity suite.
  IF v_provider = 'google_workspace' THEN
    IF company.set_integration_state('microsoft_365', 'selected', 'conflict')
       IS DISTINCT FROM 'conflict' THEN
      RAISE EXCEPTION 'concurrent suite selection was allowed';
    END IF;
  ELSIF v_provider = 'microsoft_365' THEN
    IF company.set_integration_state('google_workspace', 'selected', 'conflict')
       IS DISTINCT FROM 'conflict' THEN
      RAISE EXCEPTION 'concurrent suite selection was allowed';
    END IF;
  END IF;

  -- Organization app configuration and read-only scope enforcement.
  scopes := CASE v_provider
    WHEN 'google_workspace' THEN ARRAY['https://www.googleapis.com/auth/gmail.readonly']
    WHEN 'microsoft_365' THEN ARRAY['Mail.Read']
    ELSE NULL END;
  IF v_provider IN ('google_workspace', 'microsoft_365') THEN
    SELECT r.decision INTO decision FROM company.configure_integration_app(
      v_provider, 'ws-' || v_provider, CASE v_provider WHEN 'microsoft_365' THEN 'tenant-1' ELSE NULL END,
      'https://e.example.test/company/oauth/callback', ARRAY['Mail.ReadWrite']) r;
    IF decision IS DISTINCT FROM 'invalid_scope' THEN
      RAISE EXCEPTION 'write scope was accepted';
    END IF;
    IF (SELECT count(*) FROM company.integration_apps WHERE org_id = org_a) <> 0 THEN
      RAISE EXCEPTION 'invalid scope created an app';
    END IF;
  END IF;
  SELECT r.decision, r.app_id INTO decision, app_a FROM company.configure_integration_app(
    v_provider, 'ws-' || v_provider,
    CASE v_provider WHEN 'microsoft_365' THEN 'tenant-1' ELSE NULL END,
    CASE WHEN v_provider IN ('google_workspace', 'microsoft_365')
         THEN 'https://e.example.test/company/oauth/callback' ELSE NULL END,
    scopes) r;
  IF decision IS DISTINCT FROM 'configured' OR app_a IS NULL THEN
    RAISE EXCEPTION 'organization app configuration failed: %', decision;
  END IF;

  -- A member needs an active assistant before credentials can be bound.
  PERFORM set_config('company.member_id', owner_a::text, true);
  SELECT r.assistant_id INTO assistant_a FROM company.register_personal_assistant() r;
  IF assistant_a IS NULL THEN RAISE EXCEPTION 'assistant registration failed'; END IF;

  IF v_provider = 'slack' THEN
    PERFORM set_config('company.member_id', admin_a::text, true);
    SELECT r.decision INTO decision FROM company.bind_external_account(
      owner_a, 'slack', 'ws-slack', 'U-owner-a') r;
    IF decision IS DISTINCT FROM 'created' THEN RAISE EXCEPTION 'slack mapping failed'; END IF;
    PERFORM set_config('company.member_id', owner_a::text, true);
    SELECT r.decision, r.reason_code, r.member_id, r.assistant_id
      INTO decision, reason, member_out, assistant_out
      FROM company.slack_route_decision('ws-slack', 'U-owner-a') r;
    IF decision IS DISTINCT FROM 'allow' OR member_out IS DISTINCT FROM owner_a
       OR assistant_out IS DISTINCT FROM assistant_a THEN
      RAISE EXCEPTION 'slack routing failed: % / %', decision, reason;
    END IF;
    SELECT r.decision, r.reason_code INTO decision, reason
      FROM company.slack_route_decision('ws-slack', 'U-stranger') r;
    IF decision IS DISTINCT FROM 'denied' OR reason IS DISTINCT FROM 'unregistered' THEN
      RAISE EXCEPTION 'unregistered slack sender was accepted';
    END IF;
  ELSIF v_provider = 'notion' THEN
    PERFORM set_config('company.member_id', admin_a::text, true);
    SELECT r.decision, r.document_id INTO decision, doc_a
      FROM company.sync_notion_document('ws-notion', 'page-1', 'acl-v1', 'notion body') r;
    IF decision IS DISTINCT FROM 'synced' OR doc_a IS NULL THEN
      RAISE EXCEPTION 'notion sync failed';
    END IF;
    PERFORM set_config('company.member_id', owner_a::text, true);
    SELECT r.decision, r.reason_code INTO decision, reason
      FROM company.notion_document_decision(doc_a) r;
    IF decision IS DISTINCT FROM 'allow' THEN
      RAISE EXCEPTION 'notion candidate denied: %', reason;
    END IF;
    PERFORM set_config('company.member_id', admin_a::text, true);
    IF company.remove_notion_document('page-1') IS DISTINCT FROM 'removed' THEN
      RAISE EXCEPTION 'notion removal failed';
    END IF;
    IF (SELECT count(*) FROM company.chunks WHERE document_id = doc_a) <> 0 THEN
      RAISE EXCEPTION 'notion chunks survived deletion';
    END IF;
    PERFORM set_config('company.member_id', owner_a::text, true);
    SELECT r.decision, r.reason_code INTO decision, reason
      FROM company.notion_document_decision(doc_a) r;
    IF decision IS DISTINCT FROM 'denied' OR reason IS DISTINCT FROM 'acl_unconfirmed' THEN
      RAISE EXCEPTION 'deleted notion document stayed readable';
    END IF;
  ELSE
    -- Personal delegated token: owner-bound C05 envelope plus delegation row.
    PERFORM set_config('company.member_id', owner_a::text, true);
    SELECT r.secret_id INTO secret_a FROM company.store_personal_secret(
      v_provider, 'oauth_delegation', decode(repeat('11', 24), 'hex'),
      decode(repeat('22', 12), 'hex'), 1) r;
    IF secret_a IS NULL THEN RAISE EXCEPTION 'delegation secret storage failed'; END IF;
    SELECT r.decision, r.delegation_id INTO decision, del_a FROM company.bind_delegation(
      v_provider, 'subject-owner-a', 'ws-' || v_provider, scopes, secret_a) r;
    IF decision IS DISTINCT FROM 'created' OR del_a IS NULL THEN
      RAISE EXCEPTION 'delegation binding failed: %', decision;
    END IF;
    SELECT r.decision, r.reason_code INTO decision, reason
      FROM company.delegation_use_decision(del_a) r;
    IF decision IS DISTINCT FROM 'allow' THEN
      RAISE EXCEPTION 'owner delegation denied: %', reason;
    END IF;

    -- A second member never sees the first member's delegation or secret.
    PERFORM set_config('company.member_id', owner_b::text, true);
    IF (SELECT count(*) FROM company.integration_delegations) <> 0 THEN
      RAISE EXCEPTION 'foreign delegation was visible';
    END IF;
    SELECT r.decision, r.reason_code INTO decision, reason
      FROM company.delegation_use_decision(del_a) r;
    IF decision IS DISTINCT FROM 'denied' THEN
      RAISE EXCEPTION 'foreign delegation use was allowed';
    END IF;

    -- A member binds only a read-only scope, and only their own envelope.
    PERFORM set_config('company.member_id', owner_a::text, true);
    SELECT r.decision INTO decision FROM company.bind_delegation(
      v_provider, 'subject-bad', 'ws-' || v_provider,
      ARRAY['Mail.ReadWrite'], secret_a) r;
    IF decision IS DISTINCT FROM 'invalid_scope' THEN
      RAISE EXCEPTION 'write delegation scope was accepted';
    END IF;
    SELECT r.decision INTO decision FROM company.bind_delegation(
      v_provider, 'subject-dup', 'ws-' || v_provider, scopes, gen_random_uuid()) r;
    IF decision IS DISTINCT FROM 'secret_unavailable' THEN
      RAISE EXCEPTION 'delegation accepted an unknown secret';
    END IF;

    -- Revocation stops use immediately.
    IF company.revoke_delegation(del_a) IS DISTINCT FROM 'revoked' THEN
      RAISE EXCEPTION 'delegation revocation failed';
    END IF;
    SELECT r.decision, r.reason_code INTO decision, reason
      FROM company.delegation_use_decision(del_a) r;
    IF decision IS DISTINCT FROM 'denied' OR reason IS DISTINCT FROM 'delegation_unavailable' THEN
      RAISE EXCEPTION 'revoked delegation stayed usable';
    END IF;
    IF (SELECT state FROM company.secrets WHERE id = secret_a) <> 'revoked' THEN
      RAISE EXCEPTION 'revocation did not revoke the vault envelope';
    END IF;
  END IF;

  -- Deselecting the provider (SKIP) revokes the app and every delegation.
  PERFORM set_config('company.member_id', admin_a::text, true);
  IF company.set_integration_state(v_provider, 'skip', 'not_selected')
     IS DISTINCT FROM 'configured' THEN
    RAISE EXCEPTION 'provider deselect failed';
  END IF;
  IF company.integration_selection(v_provider) IS DISTINCT FROM 'skip' THEN
    RAISE EXCEPTION 'provider stayed selected after skip';
  END IF;
  IF (SELECT count(*) FROM company.integration_apps
      WHERE org_id = org_a AND provider = v_provider AND state = 'active') <> 0 THEN
    RAISE EXCEPTION 'skip left an active app';
  END IF;
  IF (SELECT count(*) FROM company.integration_delegations
      WHERE org_id = org_a AND provider = v_provider AND state = 'active') <> 0 THEN
    RAISE EXCEPTION 'skip left an active delegation';
  END IF;

  -- Another organization is unaffected and cannot write into A.
  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', admin_b::text, true);
  IF company.integration_selection(v_provider) IS DISTINCT FROM 'skip' THEN
    RAISE EXCEPTION 'other organization saw A selection';
  END IF;
  rejected := false;
  BEGIN
    INSERT INTO company.integration_apps(org_id, provider, workspace_id)
      VALUES (org_a, v_provider, 'ws-cross');
  EXCEPTION WHEN insufficient_privilege OR foreign_key_violation OR raise_exception THEN
    rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'cross-organization app write was accepted'; END IF;

  -- Selection and app changes are audited without credential material.
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  IF NOT EXISTS (SELECT 1 FROM company.audit_events
    WHERE org_id = org_a AND event_type = 'integration'
      AND action = 'integration_selected') THEN
    RAISE EXCEPTION 'selection audit event is missing';
  END IF;
  IF EXISTS (SELECT 1 FROM company.audit_events
    WHERE org_id = org_a AND event_type = 'integration'
      AND (metadata ? 'token' OR metadata ? 'secret' OR metadata ? 'client_secret')) THEN
    RAISE EXCEPTION 'integration audit contained credential material';
  END IF;
END;
$verify$;
ROLLBACK;
