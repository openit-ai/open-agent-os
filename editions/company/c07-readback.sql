-- C07 uses synthetic fixtures and always rolls back. No secret value is printed.
BEGIN;
DO $verify$
DECLARE
  org_a uuid := gen_random_uuid();
  org_b uuid := gen_random_uuid();
  admin_a uuid := gen_random_uuid();
  admin_b uuid := gen_random_uuid();
  owner_a uuid := gen_random_uuid();
  owner_b uuid := gen_random_uuid();
  admin_c uuid := gen_random_uuid();
  decision text;
  reason text;
  version integer;
  appr_mail uuid;
  appr_self uuid;
  appr_exp uuid;
  appr_stale uuid;
  appr_deny uuid;
  appr_b uuid;
  expires timestamptz;
  rejected boolean;
  changed integer;
  member_count integer;
BEGIN
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_a, 'c07-' || org_a::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (admin_a, org_a, 'c07-admin-a-' || admin_a::text, 'admin', 'active'),
    (admin_b, org_a, 'c07-admin-b-' || admin_b::text, 'admin', 'active'),
    (owner_a, org_a, 'c07-owner-a-' || owner_a::text, 'member', 'active'),
    (owner_b, org_a, 'c07-owner-b-' || owner_b::text, 'member', 'active');

  -- Default deny for an unknown action, and only an administrator may change rules.
  PERFORM set_config('company.member_id', owner_a::text, true);
  IF (SELECT r.decision FROM company.policy_decision('mail.send') r)
       IS DISTINCT FROM 'deny' THEN
    RAISE EXCEPTION 'unknown action was not denied';
  END IF;
  SELECT r.decision, r.reason_code INTO decision, reason
    FROM company.authorize_action('mail.send', 'res', 'ctx') r;
  IF decision IS DISTINCT FROM 'deny' OR reason IS DISTINCT FROM 'default_deny' THEN
    RAISE EXCEPTION 'default deny failed';
  END IF;
  IF (SELECT r.decision FROM company.set_policy('mail.send', 'high', 'approval') r)
       IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'non-admin changed a policy';
  END IF;
  IF (SELECT r.decision FROM company.install_default_policies() r)
       IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'non-admin installed the default policy bundle';
  END IF;

  -- Administrator installs the minimum bundle; re-applying a rule is a no-op.
  PERFORM set_config('company.member_id', admin_a::text, true);
  IF (SELECT r.policy_count FROM company.install_default_policies() r) <> 8 THEN
    RAISE EXCEPTION 'default policy bundle is incomplete';
  END IF;
  IF (SELECT r.decision FROM company.set_policy('self.chat', 'low', 'allow') r)
       IS DISTINCT FROM 'unchanged' THEN
    RAISE EXCEPTION 'identical policy change raised the version';
  END IF;

  -- Explicit allow for own chat.
  PERFORM set_config('company.member_id', owner_a::text, true);
  SELECT r.decision, r.reason_code INTO decision, reason
    FROM company.authorize_action('self.chat', 'chat-1', 'ctx-1') r;
  IF decision IS DISTINCT FROM 'allow' OR reason IS DISTINCT FROM 'policy_allow' THEN
    RAISE EXCEPTION 'explicit allow failed';
  END IF;

  -- A high-risk action needs a matching approval.
  SELECT r.decision, r.reason_code INTO decision, reason
    FROM company.authorize_action('mail.send', 'mail-1', 'ctx-1') r;
  IF decision IS DISTINCT FROM 'deny' OR reason IS DISTINCT FROM 'approval_required' THEN
    RAISE EXCEPTION 'approval requirement was not enforced';
  END IF;
  IF (SELECT r.decision FROM company.request_approval('self.chat', 'x', 'y') r)
       IS DISTINCT FROM 'not_required' THEN
    RAISE EXCEPTION 'approval was requested for an allow policy';
  END IF;
  IF (SELECT r.decision FROM company.request_approval('unknown.action', 'x', 'y') r)
       IS DISTINCT FROM 'default_deny' THEN
    RAISE EXCEPTION 'approval was requested for an unknown action';
  END IF;
  SELECT r.approval_id, r.expires_at INTO appr_mail, expires
    FROM company.request_approval('mail.send', 'mail-1', 'ctx-1') r;
  IF appr_mail IS NULL OR expires <= now() OR expires > now() + interval '11 minutes' THEN
    RAISE EXCEPTION 'approval request was not bound to a 10 minute window';
  END IF;

  -- Only an active administrator may decide, and never for their own request.
  PERFORM set_config('company.member_id', owner_b::text, true);
  IF (SELECT r.decision FROM company.decide_approval(appr_mail, true) r)
       IS DISTINCT FROM 'not_admin' THEN
    RAISE EXCEPTION 'non-admin decided an approval';
  END IF;
  PERFORM set_config('company.member_id', admin_a::text, true);
  SELECT r.approval_id INTO appr_self
    FROM company.request_approval('policy.change', 'pol-1', 'ctx-1') r;
  IF appr_self IS NULL THEN RAISE EXCEPTION 'administrator request is missing'; END IF;
  IF (SELECT r.decision FROM company.decide_approval(appr_self, true) r)
       IS DISTINCT FROM 'self_denied' THEN
    RAISE EXCEPTION 'self approval was accepted';
  END IF;
  -- Direct SQL cannot bypass the same rule.
  rejected := false;
  BEGIN
    UPDATE company.approvals SET state = 'approved', approver_id = admin_a
      WHERE id = appr_self;
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'direct SQL self approval was accepted'; END IF;
  rejected := false;
  BEGIN
    UPDATE company.approvals SET state = 'approved', approver_id = owner_a
      WHERE id = appr_self;
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'direct SQL non-admin approval was accepted'; END IF;

  -- A second administrator approves the member request, once.
  PERFORM set_config('company.member_id', admin_b::text, true);
  IF (SELECT r.decision FROM company.decide_approval(appr_mail, true) r)
       IS DISTINCT FROM 'approved' THEN
    RAISE EXCEPTION 'administrator approval failed';
  END IF;
  IF (SELECT r.decision FROM company.decide_approval(appr_mail, true) r)
       IS DISTINCT FROM 'not_pending' THEN
    RAISE EXCEPTION 'approval was decided twice';
  END IF;

  -- Binding mismatch is denied; the exact approval is consumed once, then reused.
  PERFORM set_config('company.member_id', owner_a::text, true);
  IF (SELECT r.reason_code FROM company.authorize_action('mail.send', 'other', 'ctx-1', appr_mail) r)
       IS DISTINCT FROM 'binding_mismatch' THEN
    RAISE EXCEPTION 'resource mismatch was allowed';
  END IF;
  IF (SELECT r.reason_code FROM company.authorize_action('mail.send', 'mail-1', 'other', appr_mail) r)
       IS DISTINCT FROM 'binding_mismatch' THEN
    RAISE EXCEPTION 'context mismatch was allowed';
  END IF;
  IF (SELECT r.decision FROM company.authorize_action('mail.send', 'mail-1', 'ctx-1', appr_mail) r)
       IS DISTINCT FROM 'allow' THEN
    RAISE EXCEPTION 'an approved action was not allowed';
  END IF;
  IF (SELECT r.reason_code FROM company.authorize_action('mail.send', 'mail-1', 'ctx-1', appr_mail) r)
       IS DISTINCT FROM 'reused' THEN
    RAISE EXCEPTION 'an approval was reused';
  END IF;

  -- A denied request never executes.
  SELECT r.approval_id INTO appr_deny
    FROM company.request_approval('mail.send', 'mail-2', 'ctx-1') r;
  PERFORM set_config('company.member_id', admin_b::text, true);
  IF (SELECT r.decision FROM company.decide_approval(appr_deny, false) r)
       IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'approval denial failed';
  END IF;
  PERFORM set_config('company.member_id', owner_a::text, true);
  IF (SELECT r.reason_code FROM company.authorize_action('mail.send', 'mail-2', 'ctx-1', appr_deny) r)
       IS DISTINCT FROM 'approval_denied' THEN
    RAISE EXCEPTION 'a denied approval was allowed';
  END IF;

  -- An expired approval is denied and recorded as expired.
  INSERT INTO company.approvals(org_id, requester_id, approver_id, action,
    resource_digest, context_digest, policy_version, expires_at, state)
  VALUES (org_a, owner_a, admin_b, 'mail.send', 'mail-3', 'ctx-1',
    (SELECT p.version FROM company.policies p
      WHERE p.org_id = org_a AND p.action = 'mail.send'),
    now() - interval '1 minute', 'approved') RETURNING id INTO appr_exp;
  IF (SELECT r.reason_code FROM company.authorize_action('mail.send', 'mail-3', 'ctx-1', appr_exp) r)
       IS DISTINCT FROM 'expired' THEN
    RAISE EXCEPTION 'an expired approval was allowed';
  END IF;
  IF (SELECT a.state FROM company.approvals a WHERE a.id = appr_exp)
       IS DISTINCT FROM 'expired' THEN
    RAISE EXCEPTION 'expiry was not recorded';
  END IF;

  -- A policy change invalidates a previously approved request.
  SELECT r.approval_id INTO appr_stale
    FROM company.request_approval('mail.send', 'mail-4', 'ctx-1') r;
  PERFORM set_config('company.member_id', admin_b::text, true);
  PERFORM company.decide_approval(appr_stale, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  SELECT r.policy_version INTO version
    FROM company.set_policy('mail.send', 'critical', 'approval') r;
  IF version IS NULL THEN RAISE EXCEPTION 'policy version change failed'; END IF;
  PERFORM set_config('company.member_id', owner_a::text, true);
  IF (SELECT r.reason_code FROM company.authorize_action('mail.send', 'mail-4', 'ctx-1', appr_stale) r)
       IS DISTINCT FROM 'stale_policy' THEN
    RAISE EXCEPTION 'a stale-policy approval was allowed';
  END IF;

  -- A suspended owner is not authorized.
  PERFORM set_config('company.member_id', owner_b::text, true);
  UPDATE company.members SET state = 'suspended' WHERE id = owner_b;
  IF (SELECT r.reason_code FROM company.authorize_action('self.chat', 'x', 'y') r)
       IS DISTINCT FROM 'unauthenticated' THEN
    RAISE EXCEPTION 'a suspended owner was authorized';
  END IF;

  -- Another organization never sees or decides this organization's approval.
  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', admin_c::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_b, 'c07-' || org_b::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state)
    VALUES (admin_c, org_b, 'c07-admin-c-' || admin_c::text, 'admin', 'active');
  PERFORM company.set_policy('mail.send', 'high', 'approval');
  SELECT r.approval_id INTO appr_b
    FROM company.request_approval('mail.send', 'b-1', 'c-1') r;
  IF appr_b IS NULL THEN RAISE EXCEPTION 'tenant approval is missing'; END IF;
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', owner_a::text, true);
  IF (SELECT r.reason_code FROM company.authorize_action('mail.send', 'b-1', 'c-1', appr_b) r)
       IS DISTINCT FROM 'approval_missing' THEN
    RAISE EXCEPTION 'a cross-tenant approval was visible';
  END IF;
  PERFORM set_config('company.member_id', admin_a::text, true);
  IF (SELECT r.decision FROM company.decide_approval(appr_b, true) r)
       IS DISTINCT FROM 'not_found' THEN
    RAISE EXCEPTION 'a cross-tenant approval was decidable';
  END IF;

  -- The administrator audit view covers the whole organization; a member sees
  -- only their own events. Audit rows are append-only and carry no secrets.
  IF (SELECT count(*) FROM company.query_audit_events(
        now() - interval '1 hour', now() + interval '1 minute')) = 0 THEN
    RAISE EXCEPTION 'administrator audit view was empty';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM company.query_audit_events(
        now() - interval '1 hour', now() + interval '1 minute') e
        WHERE e.event_type = 'policy' AND e.action = 'policy_changed')
     OR NOT EXISTS (SELECT 1 FROM company.query_audit_events(
        now() - interval '1 hour', now() + interval '1 minute') e
        WHERE e.event_type = 'execution' AND e.decision = 'deny') THEN
    RAISE EXCEPTION 'administrator audit view missed policy or deny events';
  END IF;
  IF EXISTS (SELECT 1 FROM company.query_audit_events(
        now() - interval '1 hour', now() + interval '1 minute') e
        WHERE e.actor_id = admin_c) THEN
    RAISE EXCEPTION 'audit view leaked another organization';
  END IF;
  PERFORM set_config('company.member_id', owner_a::text, true);
  SELECT count(*) INTO member_count FROM company.query_audit_events(
    now() - interval '1 hour', now() + interval '1 minute');
  IF member_count = 0 THEN RAISE EXCEPTION 'member audit view was empty'; END IF;
  IF EXISTS (SELECT 1 FROM company.query_audit_events(
        now() - interval '1 hour', now() + interval '1 minute') e
        WHERE e.actor_id = admin_a) THEN
    RAISE EXCEPTION 'member audit view exposed another actor';
  END IF;
  PERFORM set_config('company.member_id', admin_a::text, true);
  UPDATE company.audit_events SET decision = 'tampered' WHERE event_type = 'policy';
  GET DIAGNOSTICS changed = ROW_COUNT;
  IF changed <> 0 THEN RAISE EXCEPTION 'audit rows were mutable'; END IF;
  IF (SELECT count(*) FROM company.audit_events e
       WHERE e.metadata ?| ARRAY['ciphertext', 'nonce', 'token', 'secret', 'password',
         'key', 'body', 'snippet', 'prompt', 'api_key', 'authorization']) <> 0 THEN
    RAISE EXCEPTION 'audit metadata leaked sensitive fields';
  END IF;
  rejected := false;
  BEGIN
    INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
      resource_ref, decision, request_id, metadata)
    VALUES (org_a, admin_a, admin_a, 'fixture', 'action', 'ref', 'allow',
      'req-' || gen_random_uuid()::text, '{"token": "leak"}'::jsonb);
  EXCEPTION WHEN raise_exception THEN rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'sensitive audit metadata was accepted'; END IF;
END;
$verify$;
ROLLBACK;
