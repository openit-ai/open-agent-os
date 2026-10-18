-- C07 policy engine, just-in-time approval and audit query boundary.
-- The trusted server authenticates the Project session and sets company.org_id
-- and company.member_id for the transaction; no external input supplies owner.
-- This migration fills the reserved C07 slot (version 8).
--
-- Minimum policy bundle: no explicit rule means deny. A member may read the
-- rules; only an active administrator may change them, and every change raises
-- the policy version so existing approvals become stale.
--
-- JIT approval: a high-risk action bound to action/resource/context/policy
-- version with a 10 minute expiry. An active administrator other than the
-- requester decides it. Consumption is a single conditional UPDATE
-- (state = 'approved' AND consumed_at IS NULL AND expires_at > now()) so a
-- request can never be reused after a policy or resource change.
--
-- Audit: allow and deny decisions are written in the same transaction as the
-- action, so a failed audit denies the risky execution. Audit rows stay
-- append-only for the app role, administrators query the whole organization,
-- members only their own events, and metadata can never carry secret material.

-- 1. Policy rules: active members read, administrators change.
DROP POLICY policy_scope ON company.policies;
CREATE POLICY policy_read ON company.policies FOR SELECT USING (
  org_id::text = current_setting('company.org_id', true)
  AND company.c04_member_ready());
CREATE POLICY policy_insert ON company.policies FOR INSERT WITH CHECK (
  org_id::text = current_setting('company.org_id', true)
  AND company.c03_admin_ready());
CREATE POLICY policy_update ON company.policies FOR UPDATE USING (
  org_id::text = current_setting('company.org_id', true)
  AND company.c03_admin_ready()) WITH CHECK (
  org_id::text = current_setting('company.org_id', true)
  AND company.c03_admin_ready());

-- 2. Approvals: the requester and any active administrator of the same
-- organization can read and update; a member can only create their own request.
DROP POLICY approval_scope ON company.approvals;
CREATE POLICY approval_read ON company.approvals FOR SELECT USING (
  org_id::text = current_setting('company.org_id', true)
  AND (requester_id::text = current_setting('company.member_id', true)
       OR company.c03_admin_ready()));
CREATE POLICY approval_insert ON company.approvals FOR INSERT WITH CHECK (
  org_id::text = current_setting('company.org_id', true)
  AND requester_id::text = current_setting('company.member_id', true)
  AND company.c04_member_ready());
CREATE POLICY approval_update ON company.approvals FOR UPDATE USING (
  org_id::text = current_setting('company.org_id', true)
  AND (requester_id::text = current_setting('company.member_id', true)
       OR company.c03_admin_ready())) WITH CHECK (
  org_id::text = current_setting('company.org_id', true)
  AND (requester_id::text = current_setting('company.member_id', true)
       OR company.c03_admin_ready()));

-- 3. A direct-SQL state machine keeps approvals honest even outside the helper
-- functions: the binding is immutable, only an active administrator other than
-- the requester may decide, and terminal states cannot be reopened.
CREATE FUNCTION company.c07_approval_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
  IF NEW.org_id IS DISTINCT FROM OLD.org_id
     OR NEW.requester_id IS DISTINCT FROM OLD.requester_id
     OR NEW.action IS DISTINCT FROM OLD.action
     OR NEW.resource_digest IS DISTINCT FROM OLD.resource_digest
     OR NEW.context_digest IS DISTINCT FROM OLD.context_digest
     OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
     OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
    RAISE EXCEPTION 'approval request binding is immutable';
  END IF;
  IF OLD.state = 'pending' THEN
    IF NEW.state IN ('approved', 'denied') THEN
      IF NEW.approver_id IS NULL THEN
        RAISE EXCEPTION 'approval decision requires an approver';
      END IF;
      IF NEW.approver_id = OLD.requester_id THEN
        RAISE EXCEPTION 'self approval is forbidden';
      END IF;
      IF NEW.approver_id::text <> current_setting('company.member_id', true)
         OR NOT company.c03_admin_ready()
         OR NOT EXISTS (SELECT 1 FROM company.members m
              WHERE m.org_id = NEW.org_id AND m.id = NEW.approver_id
                AND m.role = 'admin' AND m.state = 'active') THEN
        RAISE EXCEPTION 'only an active administrator may decide an approval';
      END IF;
      IF NEW.consumed_at IS NOT NULL THEN
        RAISE EXCEPTION 'a decision cannot be consumed';
      END IF;
    ELSIF NEW.state = 'expired' THEN
      IF NEW.approver_id IS DISTINCT FROM OLD.approver_id THEN
        RAISE EXCEPTION 'expiry cannot set an approver';
      END IF;
    ELSIF NEW.state <> 'pending' THEN
      RAISE EXCEPTION 'invalid approval transition';
    END IF;
  ELSIF OLD.state = 'approved' THEN
    IF NEW.state = 'consumed' THEN
      IF NEW.consumed_at IS NULL THEN
        RAISE EXCEPTION 'consumption requires a timestamp';
      END IF;
      IF NEW.approver_id IS DISTINCT FROM OLD.approver_id THEN
        RAISE EXCEPTION 'consumption cannot change the approver';
      END IF;
      IF NOT company.c03_admin_ready()
         AND OLD.requester_id::text <> current_setting('company.member_id', true) THEN
        RAISE EXCEPTION 'only the requester or an administrator may consume';
      END IF;
    ELSIF NEW.state = 'expired' THEN
      IF NEW.approver_id IS DISTINCT FROM OLD.approver_id THEN
        RAISE EXCEPTION 'expiry cannot set an approver';
      END IF;
    ELSE
      RAISE EXCEPTION 'invalid approval transition';
    END IF;
  ELSE
    RAISE EXCEPTION 'approval is final';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER approvals_c07_guard BEFORE UPDATE ON company.approvals
  FOR EACH ROW EXECUTE FUNCTION company.c07_approval_guard();

-- 4. Audit rows must never carry secret material or raw content.
CREATE FUNCTION company.c07_audit_metadata_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE forbidden text;
BEGIN
  SELECT key INTO forbidden FROM jsonb_object_keys(NEW.metadata) AS key
    WHERE key IN ('ciphertext', 'nonce', 'token', 'secret', 'password', 'key',
                  'body', 'snippet', 'prompt', 'api_key', 'authorization',
                  'cookie', 'csrf')
    LIMIT 1;
  IF forbidden IS NOT NULL THEN
    RAISE EXCEPTION 'audit metadata must not contain sensitive fields';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER audit_events_c07_guard BEFORE INSERT ON company.audit_events
  FOR EACH ROW EXECUTE FUNCTION company.c07_audit_metadata_guard();

-- 5. Append-only audit helper used by every C07 decision. SECURITY INVOKER, so
-- the C05 audit_insert policy still binds the actor to the session member.
CREATE FUNCTION company.c07_record_audit(p_event_type text, p_action text,
  p_resource text, p_decision text, p_reason text, p_request text, p_metadata jsonb)
RETURNS void LANGUAGE sql VOLATILE SECURITY INVOKER AS $$
  INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
    resource_ref, decision, reason_code, request_id, metadata)
  VALUES (current_setting('company.org_id', true)::uuid,
    NULLIF(current_setting('company.member_id', true), '')::uuid,
    NULLIF(current_setting('company.member_id', true), '')::uuid,
    p_event_type, p_action, p_resource, p_decision, p_reason,
    COALESCE(p_request, gen_random_uuid()::text),
    COALESCE(p_metadata, '{}'::jsonb));
$$;

-- 6. Policy evaluation. An unknown action or a non-member is denied.
CREATE FUNCTION company.policy_decision(p_action text)
RETURNS TABLE(decision text, policy_version integer, risk text)
LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
DECLARE rule company.policies%ROWTYPE;
BEGIN
  IF p_action IS NULL OR p_action = '' OR NOT company.c04_member_ready() THEN
    RETURN QUERY SELECT 'deny'::text, 0, 'unknown'::text; RETURN;
  END IF;
  SELECT * INTO rule FROM company.policies p
    WHERE p.org_id::text = current_setting('company.org_id', true)
      AND p.action = p_action;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'deny'::text, 0, 'unknown'::text; RETURN;
  END IF;
  RETURN QUERY SELECT rule.decision, rule.version, rule.risk;
END;
$$;

-- 7. Administrator policy change. Re-applying the same rule is a no-op; every
-- real change raises the version and writes an audit event in one transaction.
CREATE FUNCTION company.set_policy(p_action text, p_risk text, p_decision text)
RETURNS TABLE(decision text, policy_version integer)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE existing company.policies%ROWTYPE;
DECLARE saved company.policies%ROWTYPE;
BEGIN
  IF NOT company.c03_admin_ready()
     OR p_action IS NULL OR p_action = ''
     OR p_risk IS NULL OR p_risk = ''
     OR p_decision IS NULL
     OR p_decision NOT IN ('deny', 'allow', 'approval') THEN
    RETURN QUERY SELECT 'denied'::text, NULL::integer; RETURN;
  END IF;
  SELECT * INTO existing FROM company.policies p
    WHERE p.org_id::text = current_setting('company.org_id', true)
      AND p.action = p_action;
  IF FOUND THEN
    IF existing.risk = p_risk AND existing.decision = p_decision THEN
      RETURN QUERY SELECT 'unchanged'::text, existing.version; RETURN;
    END IF;
    UPDATE company.policies p SET risk = p_risk, decision = p_decision,
      version = existing.version + 1
      WHERE p.id = existing.id RETURNING * INTO saved;
  ELSE
    INSERT INTO company.policies(org_id, action, risk, decision, version)
      VALUES (current_setting('company.org_id', true)::uuid, p_action, p_risk,
        p_decision, 1) RETURNING * INTO saved;
  END IF;
  PERFORM company.c07_record_audit('policy', 'policy_changed', saved.action,
    'allow', NULL, gen_random_uuid()::text,
    jsonb_build_object('risk', saved.risk, 'decision', saved.decision,
      'policy_version', saved.version));
  RETURN QUERY SELECT 'changed'::text, saved.version;
END;
$$;

-- 8. Minimum policy bundle from the design: own chat and read search are
-- explicit allow; outbound mail, document edits, external sends and policy
-- changes need approval; unattended and unregistered tools are denied.
CREATE FUNCTION company.install_default_policies()
RETURNS TABLE(decision text, policy_count integer)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE seeds text[][] := ARRAY[
    ['self.chat', 'low', 'allow'],
    ['knowledge.read', 'low', 'allow'],
    ['mail.send', 'high', 'approval'],
    ['document.modify', 'high', 'approval'],
    ['external.send', 'high', 'approval'],
    ['policy.change', 'high', 'approval'],
    ['unattended.run', 'high', 'deny'],
    ['tool.unregistered', 'high', 'deny']];
DECLARE i integer;
DECLARE total integer := 0;
BEGIN
  IF NOT company.c03_admin_ready() THEN
    RETURN QUERY SELECT 'denied'::text, NULL::integer; RETURN;
  END IF;
  FOR i IN 1 .. array_length(seeds, 1) LOOP
    PERFORM company.set_policy(seeds[i][1], seeds[i][2], seeds[i][3]);
    total := total + 1;
  END LOOP;
  RETURN QUERY SELECT 'installed'::text, total;
END;
$$;

-- 9. Just-in-time approval request, bound to 10 minutes and the current policy.
CREATE FUNCTION company.request_approval(p_action text, p_resource_digest text,
  p_context_digest text)
RETURNS TABLE(decision text, approval_id uuid, expires_at timestamptz)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE rule company.policies%ROWTYPE;
DECLARE created company.approvals%ROWTYPE;
BEGIN
  IF NOT company.c04_member_ready()
     OR p_action IS NULL OR p_action = ''
     OR p_resource_digest IS NULL OR p_resource_digest = ''
     OR p_context_digest IS NULL OR p_context_digest = '' THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::timestamptz; RETURN;
  END IF;
  SELECT * INTO rule FROM company.policies p
    WHERE p.org_id::text = current_setting('company.org_id', true)
      AND p.action = p_action;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'default_deny'::text, NULL::uuid, NULL::timestamptz; RETURN;
  END IF;
  IF rule.decision <> 'approval' THEN
    RETURN QUERY SELECT 'not_required'::text, NULL::uuid, NULL::timestamptz; RETURN;
  END IF;
  INSERT INTO company.approvals(org_id, requester_id, action, resource_digest,
    context_digest, policy_version, expires_at)
  VALUES (current_setting('company.org_id', true)::uuid,
    current_setting('company.member_id', true)::uuid, p_action,
    p_resource_digest, p_context_digest, rule.version, now() + interval '10 minutes')
  RETURNING * INTO created;
  PERFORM company.c07_record_audit('approval', 'approval_requested',
    created.id::text, 'pending', NULL, created.id::text,
    jsonb_build_object('action', p_action, 'policy_version', rule.version));
  RETURN QUERY SELECT 'created'::text, created.id, created.expires_at;
END;
$$;

-- 10. Administrator decision. A requester can never approve their own request.
CREATE FUNCTION company.decide_approval(p_approval_id uuid, p_approve boolean)
RETURNS TABLE(decision text, approval_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE whois uuid;
DECLARE appr company.approvals%ROWTYPE;
BEGIN
  whois := NULLIF(current_setting('company.member_id', true), '')::uuid;
  IF NOT company.c03_admin_ready() THEN
    RETURN QUERY SELECT 'not_admin'::text, NULL::uuid; RETURN;
  END IF;
  SELECT * INTO appr FROM company.approvals a
    WHERE a.id = p_approval_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'not_found'::text, NULL::uuid; RETURN;
  END IF;
  IF appr.requester_id = whois THEN
    RETURN QUERY SELECT 'self_denied'::text, appr.id; RETURN;
  END IF;
  IF appr.state <> 'pending' THEN
    RETURN QUERY SELECT 'not_pending'::text, appr.id; RETURN;
  END IF;
  IF appr.expires_at <= now() THEN
    UPDATE company.approvals SET state = 'expired'
      WHERE id = appr.id AND state = 'pending';
    PERFORM company.c07_record_audit('approval', 'approval_expired', appr.id::text,
      'deny', 'expired', appr.id::text,
      jsonb_build_object('action', appr.action));
    RETURN QUERY SELECT 'expired'::text, appr.id; RETURN;
  END IF;
  UPDATE company.approvals
    SET state = CASE WHEN p_approve THEN 'approved' ELSE 'denied' END,
        approver_id = whois
    WHERE id = appr.id RETURNING * INTO appr;
  PERFORM company.c07_record_audit('approval',
    CASE WHEN p_approve THEN 'approval_approved' ELSE 'approval_denied' END,
    appr.id::text, CASE WHEN p_approve THEN 'allow' ELSE 'deny' END, NULL,
    appr.id::text, jsonb_build_object('action', appr.action,
      'policy_version', appr.policy_version));
  RETURN QUERY SELECT appr.state::text, appr.id;
END;
$$;

-- 11. The single execution gate. A policy allow passes, a deny fails, and an
-- approval rule requires a matching, unexpired, unconsumed approval that is
-- consumed atomically here. Every outcome is audited in this transaction.
CREATE FUNCTION company.authorize_action(p_action text, p_resource_digest text,
  p_context_digest text, p_approval_id uuid DEFAULT NULL)
RETURNS TABLE(decision text, reason_code text, policy_version integer,
  approval_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE whois uuid;
DECLARE rule company.policies%ROWTYPE;
DECLARE appr company.approvals%ROWTYPE;
DECLARE approval_ref uuid;
BEGIN
  whois := NULLIF(current_setting('company.member_id', true), '')::uuid;
  IF p_action IS NULL OR p_action = '' OR NOT company.c04_member_ready() THEN
    -- No authenticated actor means there is nobody to attribute an audit row to.
    RETURN QUERY SELECT 'deny'::text, 'unauthenticated'::text, 0, NULL::uuid; RETURN;
  END IF;
  SELECT * INTO rule FROM company.policies p
    WHERE p.org_id::text = current_setting('company.org_id', true)
      AND p.action = p_action;
  IF NOT FOUND THEN
    PERFORM company.c07_record_audit('execution', 'action_denied', p_action,
      'deny', 'default_deny', gen_random_uuid()::text,
      jsonb_build_object('action', p_action));
    RETURN QUERY SELECT 'deny'::text, 'default_deny'::text, 0, NULL::uuid; RETURN;
  END IF;
  IF rule.decision = 'deny' THEN
    PERFORM company.c07_record_audit('execution', 'action_denied', p_action,
      'deny', 'policy_deny', gen_random_uuid()::text,
      jsonb_build_object('action', p_action, 'policy_version', rule.version));
    RETURN QUERY SELECT 'deny'::text, 'policy_deny'::text, rule.version, NULL::uuid;
    RETURN;
  END IF;
  IF rule.decision = 'allow' THEN
    PERFORM company.c07_record_audit('execution', 'action_allowed', p_action,
      'allow', 'policy_allow', gen_random_uuid()::text,
      jsonb_build_object('action', p_action, 'policy_version', rule.version));
    RETURN QUERY SELECT 'allow'::text, 'policy_allow'::text, rule.version,
      NULL::uuid; RETURN;
  END IF;

  IF p_approval_id IS NULL THEN
    PERFORM company.c07_record_audit('execution', 'action_denied', p_action,
      'deny', 'approval_required', gen_random_uuid()::text,
      jsonb_build_object('action', p_action, 'policy_version', rule.version));
    RETURN QUERY SELECT 'deny'::text, 'approval_required'::text, rule.version,
      NULL::uuid; RETURN;
  END IF;
  SELECT * INTO appr FROM company.approvals a
    WHERE a.id = p_approval_id AND a.requester_id = whois FOR UPDATE;
  IF NOT FOUND THEN
    PERFORM company.c07_record_audit('execution', 'action_denied', p_action,
      'deny', 'approval_missing', p_approval_id::text,
      jsonb_build_object('action', p_action, 'approval_id', p_approval_id));
    RETURN QUERY SELECT 'deny'::text, 'approval_missing'::text, rule.version,
      p_approval_id; RETURN;
  END IF;
  IF appr.action <> p_action OR appr.resource_digest <> p_resource_digest
     OR appr.context_digest <> p_context_digest THEN
    PERFORM company.c07_record_audit('execution', 'action_denied', p_action,
      'deny', 'binding_mismatch', appr.id::text,
      jsonb_build_object('action', p_action, 'approval_id', appr.id));
    RETURN QUERY SELECT 'deny'::text, 'binding_mismatch'::text, rule.version,
      appr.id; RETURN;
  END IF;
  IF appr.policy_version <> rule.version THEN
    PERFORM company.c07_record_audit('execution', 'action_denied', p_action,
      'deny', 'stale_policy', appr.id::text,
      jsonb_build_object('action', p_action, 'approval_id', appr.id,
        'policy_version', rule.version,
        'approval_policy_version', appr.policy_version));
    RETURN QUERY SELECT 'deny'::text, 'stale_policy'::text, rule.version,
      appr.id; RETURN;
  END IF;
  IF appr.state = 'consumed' THEN
    PERFORM company.c07_record_audit('execution', 'action_denied', p_action,
      'deny', 'reused', appr.id::text,
      jsonb_build_object('action', p_action, 'approval_id', appr.id));
    RETURN QUERY SELECT 'deny'::text, 'reused'::text, rule.version, appr.id;
    RETURN;
  END IF;
  IF appr.state = 'denied' THEN
    PERFORM company.c07_record_audit('execution', 'action_denied', p_action,
      'deny', 'approval_denied', appr.id::text,
      jsonb_build_object('action', p_action, 'approval_id', appr.id));
    RETURN QUERY SELECT 'deny'::text, 'approval_denied'::text, rule.version,
      appr.id; RETURN;
  END IF;
  IF appr.state = 'expired' THEN
    PERFORM company.c07_record_audit('execution', 'action_denied', p_action,
      'deny', 'expired', appr.id::text,
      jsonb_build_object('action', p_action, 'approval_id', appr.id));
    RETURN QUERY SELECT 'deny'::text, 'expired'::text, rule.version, appr.id;
    RETURN;
  END IF;
  IF appr.state <> 'approved' THEN
    PERFORM company.c07_record_audit('execution', 'action_denied', p_action,
      'deny', 'approval_pending', appr.id::text,
      jsonb_build_object('action', p_action, 'approval_id', appr.id));
    RETURN QUERY SELECT 'deny'::text, 'approval_pending'::text, rule.version,
      appr.id; RETURN;
  END IF;

  -- Single conditional consume. An approval that is already expired is marked
  -- expired first; a row that is no longer approved or already consumed matches
  -- nothing and the action is denied below. approval_ref survives a failed
  -- UPDATE ... RETURNING, which would otherwise null out the row variable.
  approval_ref := appr.id;
  IF appr.expires_at <= now() THEN
    UPDATE company.approvals a SET state = 'expired'
      WHERE a.id = approval_ref AND a.state = 'approved';
    PERFORM company.c07_record_audit('execution', 'action_denied', p_action,
      'deny', 'expired', approval_ref::text,
      jsonb_build_object('action', p_action, 'approval_id', approval_ref));
    RETURN QUERY SELECT 'deny'::text, 'expired'::text, rule.version,
      approval_ref;
    RETURN;
  END IF;
  UPDATE company.approvals a SET state = 'consumed', consumed_at = now()
    WHERE a.id = approval_ref AND a.state = 'approved' AND a.consumed_at IS NULL
      AND a.expires_at > now()
    RETURNING * INTO appr;
  IF NOT FOUND THEN
    UPDATE company.approvals a SET state = 'expired'
      WHERE a.id = approval_ref AND a.state = 'approved' AND a.expires_at <= now();
    PERFORM company.c07_record_audit('execution', 'action_denied', p_action,
      'deny', 'expired', approval_ref::text,
      jsonb_build_object('action', p_action, 'approval_id', approval_ref));
    RETURN QUERY SELECT 'deny'::text, 'expired'::text, rule.version,
      approval_ref;
    RETURN;
  END IF;
  PERFORM company.c07_record_audit('execution', 'action_allowed', p_action,
    'allow', 'approval_consumed', approval_ref::text,
    jsonb_build_object('action', p_action, 'policy_version', rule.version,
      'approval_id', approval_ref));
  RETURN QUERY SELECT 'allow'::text, 'approval_consumed'::text, rule.version,
    approval_ref;
END;
$$;

-- 12. Audit query boundary. RLS already limits rows: an active administrator
-- sees the whole organization, a member only rows where they are actor or
-- owner. Metadata is deliberately not returned so no sensitive field can leak.
CREATE FUNCTION company.query_audit_events(p_since timestamptz, p_until timestamptz)
RETURNS TABLE(event_id uuid, at timestamptz, actor_id uuid, owner_id uuid,
  event_type text, action text, resource_ref text, decision text,
  reason_code text, request_id text)
LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT e.id, e.at, e.actor_id, e.owner_id, e.event_type, e.action,
    e.resource_ref, e.decision, e.reason_code, e.request_id
  FROM company.audit_events e
  WHERE e.at >= p_since AND e.at <= p_until
  ORDER BY e.at, e.id;
$$;
