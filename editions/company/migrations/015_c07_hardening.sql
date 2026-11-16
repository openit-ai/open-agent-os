-- oaos-applied-with: 8
-- C07 policy, JIT approval and audit hardening. Applied by C07 and every later
-- phase. Fixes audit findings:
--   P1-1  a member could INSERT an already approved request (any admin as
--         approver, any expiry); now every request starts pending, unbound to
--         an approver, inside the 10 minute window and the current policy;
--   P1-10 the audit metadata guard checked only top-level, case-sensitive keys;
--   P2-17 a direct policy UPDATE kept the version and wrote no audit row, so
--         earlier approvals stayed valid;
--   P2-18 set_policy changed rules without a consumed policy.change approval;
--   P2-34 request_id was a fresh random value per event instead of the request.

-- 1. Approval requests: BEFORE INSERT enforcement for direct SQL as well as
--    request_approval(). A shorter expiry is accepted (stricter), never longer.
CREATE FUNCTION company.c07_approval_insert_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE rule company.policies%ROWTYPE;
BEGIN
  IF NEW.state IS DISTINCT FROM 'pending' OR NEW.approver_id IS NOT NULL
     OR NEW.consumed_at IS NOT NULL THEN
    RAISE EXCEPTION 'an approval request must start pending without approver or consumption';
  END IF;
  IF NEW.requester_id::text IS DISTINCT FROM current_setting('company.member_id', true)
     OR NEW.org_id::text IS DISTINCT FROM current_setting('company.org_id', true) THEN
    RAISE EXCEPTION 'an approval request belongs to the session member';
  END IF;
  SELECT * INTO rule FROM company.policies p
    WHERE p.org_id = NEW.org_id AND p.action = NEW.action;
  IF NOT FOUND OR rule.decision <> 'approval' OR rule.version <> NEW.policy_version THEN
    RAISE EXCEPTION 'an approval request must match the current approval policy';
  END IF;
  NEW.expires_at := LEAST(COALESCE(NEW.expires_at, now() + interval '10 minutes'),
                          now() + interval '10 minutes');
  RETURN NEW;
END;
$$;
CREATE TRIGGER approvals_c07_insert_guard BEFORE INSERT ON company.approvals
  FOR EACH ROW EXECUTE FUNCTION company.c07_approval_insert_guard();

-- 2. Sensitive audit metadata: any nesting depth, any case, common spellings.
CREATE FUNCTION company.c07_sensitive_name(p_name text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT n IN ('ciphertext', 'nonce', 'token', 'secret', 'password', 'passwd',
               'key', 'body', 'snippet', 'prompt', 'apikey', 'authorization',
               'cookie', 'csrf', 'privatekey')
      OR n ~ '(token|secret|password|apikey|privatekey)$'
    FROM (SELECT regexp_replace(lower(COALESCE(p_name, '')), '[^a-z0-9]', '', 'g') AS n) x;
$$;

CREATE FUNCTION company.c07_sensitive_key(p_value jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  k text;
  v jsonb;
  found text;
BEGIN
  IF p_value IS NULL THEN RETURN NULL; END IF;
  IF jsonb_typeof(p_value) = 'object' THEN
    FOR k, v IN SELECT e.key, e.value FROM jsonb_each(p_value) e LOOP
      IF company.c07_sensitive_name(k) THEN RETURN k; END IF;
      found := company.c07_sensitive_key(v);
      IF found IS NOT NULL THEN RETURN found; END IF;
    END LOOP;
  ELSIF jsonb_typeof(p_value) = 'array' THEN
    FOR v IN SELECT e.value FROM jsonb_array_elements(p_value) e LOOP
      found := company.c07_sensitive_key(v);
      IF found IS NOT NULL THEN RETURN found; END IF;
    END LOOP;
  END IF;
  RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION company.c07_audit_metadata_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
  IF company.c07_sensitive_key(NEW.metadata) IS NOT NULL THEN
    RAISE EXCEPTION 'audit metadata must not contain sensitive fields';
  END IF;
  RETURN NEW;
END;
$$;

-- 3. One request_id per request. The trusted server sets company.request_id
--    for each HTTP request and every audit row written while it is set carries
--    it, whatever id the writing function proposed. Without it, an explicit id
--    is kept and a missing one becomes a transaction-local value shared by the
--    following events of that transaction.
CREATE FUNCTION company.request_id() RETURNS text
LANGUAGE plpgsql VOLATILE AS $$
DECLARE value text := NULLIF(current_setting('company.request_id', true), '');
BEGIN
  IF value IS NULL THEN
    -- Separate name: a generated fallback must not look server-provided.
    value := NULLIF(current_setting('company.request_id_fallback', true), '');
  END IF;
  IF value IS NULL THEN
    value := gen_random_uuid()::text;
    PERFORM set_config('company.request_id_fallback', value, true);
  END IF;
  RETURN value;
END;
$$;
CREATE FUNCTION company.c07_audit_request_id() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NULLIF(current_setting('company.request_id', true), '') IS NOT NULL
     OR NULLIF(NEW.request_id, '') IS NULL THEN
    NEW.request_id := company.request_id();
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER audit_events_c07_request_id BEFORE INSERT ON company.audit_events
  FOR EACH ROW EXECUTE FUNCTION company.c07_audit_request_id();

CREATE OR REPLACE FUNCTION company.c07_record_audit(p_event_type text, p_action text,
  p_resource text, p_decision text, p_reason text, p_request text, p_metadata jsonb)
RETURNS void LANGUAGE sql VOLATILE SECURITY INVOKER AS $$
  INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
    resource_ref, decision, reason_code, request_id, metadata)
  VALUES (current_setting('company.org_id', true)::uuid,
    NULLIF(current_setting('company.member_id', true), '')::uuid,
    NULLIF(current_setting('company.member_id', true), '')::uuid,
    p_event_type, p_action, p_resource, p_decision, p_reason,
    COALESCE(NULLIF(p_request, ''), company.request_id()),
    COALESCE(p_metadata, '{}'::jsonb));
$$;

-- 4. Seed the G13 usage-billing approval rule (migration 016) into every
--    organization that already installed the policy bundle, before the change
--    guard below exists. Owner-only, this transaction only.
ALTER TABLE company.policies NO FORCE ROW LEVEL SECURITY;
INSERT INTO company.policies(org_id, action, risk, decision, version)
  SELECT p.org_id, 'llm.billing.usage', 'high', 'approval', 1
    FROM company.policies p
   WHERE p.action = 'policy.change'
     AND NOT EXISTS (SELECT 1 FROM company.policies q
                      WHERE q.org_id = p.org_id AND q.action = 'llm.billing.usage');
ALTER TABLE company.policies FORCE ROW LEVEL SECURITY;

-- 5. Every policy change, through set_policy() or direct SQL, raises the
--    version and is audited in the same transaction. Once an organization has
--    its policy.change rule, a change also requires a policy.change approval
--    for exactly this action/risk/decision that was consumed in this
--    transaction by the same administrator. policy.change itself stays
--    approval-gated so the rule cannot be switched off.
CREATE FUNCTION company.policy_change_digest(p_action text, p_risk text, p_decision text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT encode(sha256(convert_to(
    COALESCE(p_action, '') || '|' || COALESCE(p_risk, '') || '|' || COALESCE(p_decision, ''),
    'UTF8')), 'hex');
$$;

CREATE FUNCTION company.c07_policy_change_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NEW.action IS DISTINCT FROM OLD.action THEN
      RAISE EXCEPTION 'policy action is immutable';
    END IF;
    IF NEW.risk = OLD.risk AND NEW.decision = OLD.decision THEN
      NEW.version := OLD.version;
      RETURN NEW;
    END IF;
  END IF;
  IF NEW.action = 'policy.change' AND NEW.decision <> 'approval' THEN
    RAISE EXCEPTION 'policy.change must remain approval-gated';
  END IF;
  IF EXISTS (SELECT 1 FROM company.policies p
              WHERE p.org_id = NEW.org_id AND p.action = 'policy.change')
     AND NOT EXISTS (
       SELECT 1 FROM company.approvals a
        WHERE a.org_id = NEW.org_id AND a.action = 'policy.change'
          AND a.state = 'consumed' AND a.consumed_at = now()
          AND a.requester_id::text = current_setting('company.member_id', true)
          AND a.resource_digest = company.policy_change_digest(NEW.action, NEW.risk, NEW.decision)) THEN
    RAISE EXCEPTION 'policy change requires a consumed policy.change approval';
  END IF;
  IF TG_OP = 'UPDATE' THEN
    NEW.version := OLD.version + 1;
  ELSE
    NEW.version := 1;
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER policies_c07_change_guard BEFORE INSERT OR UPDATE ON company.policies
  FOR EACH ROW EXECUTE FUNCTION company.c07_policy_change_guard();

CREATE FUNCTION company.c07_policy_change_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.version = OLD.version THEN
    RETURN NEW;
  END IF;
  PERFORM company.c07_record_audit('policy', 'policy_changed', NEW.action,
    'allow', NULL, NULL,
    jsonb_build_object('risk', NEW.risk, 'decision', NEW.decision,
      'policy_version', NEW.version));
  RETURN NEW;
END;
$$;
CREATE TRIGGER policies_c07_change_audit AFTER INSERT OR UPDATE ON company.policies
  FOR EACH ROW EXECUTE FUNCTION company.c07_policy_change_audit();

-- 6. set_policy consumes the matching policy.change approval through the C07
--    execution gate. The version and the audit row come from the triggers.
DROP FUNCTION company.set_policy(text, text, text);
CREATE FUNCTION company.set_policy(p_action text, p_risk text, p_decision text,
  p_approval_id uuid DEFAULT NULL)
RETURNS TABLE(decision text, policy_version integer)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE existing company.policies%ROWTYPE;
DECLARE saved company.policies%ROWTYPE;
DECLARE gate_decision text;
DECLARE gate_reason text;
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
  IF FOUND AND existing.risk = p_risk AND existing.decision = p_decision THEN
    RETURN QUERY SELECT 'unchanged'::text, existing.version; RETURN;
  END IF;
  IF EXISTS (SELECT 1 FROM company.policies p
              WHERE p.org_id::text = current_setting('company.org_id', true)
                AND p.action = 'policy.change') THEN
    IF p_approval_id IS NULL THEN
      RETURN QUERY SELECT 'approval_required'::text, NULL::integer; RETURN;
    END IF;
    SELECT g.decision, g.reason_code INTO gate_decision, gate_reason
      FROM company.authorize_action('policy.change',
        company.policy_change_digest(p_action, p_risk, p_decision),
        'policy.change', p_approval_id) g;
    IF gate_decision IS DISTINCT FROM 'allow' THEN
      RETURN QUERY SELECT COALESCE(gate_reason, 'denied')::text, NULL::integer; RETURN;
    END IF;
  END IF;
  IF existing.id IS NOT NULL THEN
    UPDATE company.policies p SET risk = p_risk, decision = p_decision
      WHERE p.id = existing.id RETURNING * INTO saved;
  ELSE
    INSERT INTO company.policies(org_id, action, risk, decision, version)
      VALUES (current_setting('company.org_id', true)::uuid, p_action, p_risk,
        p_decision, 1) RETURNING * INTO saved;
  END IF;
  RETURN QUERY SELECT 'changed'::text, saved.version;
END;
$$;

-- 7. Default bundle: installed once per organization, policy.change last so the
--    bundle itself needs no approval. Later changes go through set_policy().
CREATE OR REPLACE FUNCTION company.install_default_policies()
RETURNS TABLE(decision text, policy_count integer)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE seeds text[][] := ARRAY[
    ['self.chat', 'low', 'allow'],
    ['knowledge.read', 'low', 'allow'],
    ['mail.send', 'high', 'approval'],
    ['document.modify', 'high', 'approval'],
    ['external.send', 'high', 'approval'],
    ['llm.billing.usage', 'high', 'approval'],
    ['unattended.run', 'high', 'deny'],
    ['tool.unregistered', 'high', 'deny'],
    ['policy.change', 'high', 'approval']];
DECLARE i integer;
DECLARE total integer := 0;
BEGIN
  IF NOT company.c03_admin_ready() THEN
    RETURN QUERY SELECT 'denied'::text, NULL::integer; RETURN;
  END IF;
  IF EXISTS (SELECT 1 FROM company.policies p
              WHERE p.org_id::text = current_setting('company.org_id', true)
                AND p.action = 'policy.change') THEN
    RETURN QUERY SELECT 'already_installed'::text,
      (SELECT count(*)::integer FROM company.policies p
        WHERE p.org_id::text = current_setting('company.org_id', true));
    RETURN;
  END IF;
  FOR i IN 1 .. array_length(seeds, 1) LOOP
    PERFORM company.set_policy(seeds[i][1], seeds[i][2], seeds[i][3]);
    total := total + 1;
  END LOOP;
  RETURN QUERY SELECT 'installed'::text, total;
END;
$$;
