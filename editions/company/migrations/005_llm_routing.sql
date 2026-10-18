-- C16/G13 routes work by task between a low-cost standard model and an
-- optional premium model. Usage billing is only usable after an administrator
-- approves it and sets a positive monthly cap. API keys stay in the Company
-- EnvironmentFile; this schema stores no credential.
CREATE TABLE company.llm_models (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  provider text NOT NULL CHECK (provider <> ''),
  model_name text NOT NULL CHECK (model_name <> ''),
  tier text NOT NULL CHECK (tier IN ('standard', 'premium')),
  billing_mode text NOT NULL CHECK (billing_mode IN ('flat', 'usage')),
  monthly_cap_usd numeric(12,2) NOT NULL DEFAULT 0 CHECK (monthly_cap_usd >= 0),
  billing_approved_at timestamptz,
  billing_approved_by uuid,
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'revoked')),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, billing_approved_by) REFERENCES company.members(org_id, id),
  UNIQUE (org_id, id)
);
CREATE UNIQUE INDEX llm_models_live_model ON company.llm_models(org_id, provider, model_name)
  WHERE state = 'active';

CREATE TABLE company.llm_routes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  task text NOT NULL CHECK (task <> ''),
  primary_model_id uuid NOT NULL,
  fallback_model_id uuid,
  updated_by uuid,
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, primary_model_id) REFERENCES company.llm_models(org_id, id),
  FOREIGN KEY (org_id, fallback_model_id) REFERENCES company.llm_models(org_id, id),
  FOREIGN KEY (org_id, updated_by) REFERENCES company.members(org_id, id),
  CHECK (fallback_model_id IS NULL OR fallback_model_id <> primary_model_id),
  UNIQUE (org_id, task)
);

CREATE TABLE company.llm_usage (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  owner_id uuid NOT NULL,
  actor_id uuid NOT NULL,
  model_id uuid NOT NULL,
  task text NOT NULL CHECK (task <> ''),
  outcome text NOT NULL CHECK (outcome IN ('success', 'failure', 'fallback', 'blocked')),
  units integer NOT NULL DEFAULT 0 CHECK (units >= 0),
  cost_usd numeric(12,4) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
  request_id text NOT NULL CHECK (request_id <> ''),
  at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, owner_id) REFERENCES company.members(org_id, id),
  FOREIGN KEY (org_id, actor_id) REFERENCES company.members(org_id, id),
  FOREIGN KEY (org_id, model_id) REFERENCES company.llm_models(org_id, id)
);

-- Organization-level monthly spend per usage-billed model. The aggregate lives
-- here so a member request can enforce the organization cap without reading
-- another owner's usage rows under RLS.
CREATE TABLE company.llm_budget_counters (
  org_id uuid NOT NULL,
  model_id uuid NOT NULL,
  month_key text NOT NULL CHECK (month_key ~ '^[0-9]{4}-[0-9]{2}$'),
  spend_usd numeric(14,4) NOT NULL DEFAULT 0 CHECK (spend_usd >= 0),
  PRIMARY KEY (org_id, model_id, month_key),
  FOREIGN KEY (org_id, model_id) REFERENCES company.llm_models(org_id, id)
);

CREATE TABLE company.llm_settings (
  org_id uuid PRIMARY KEY REFERENCES company.organizations(id),
  g13_state text NOT NULL DEFAULT 'skip' CHECK (g13_state IN ('skip', 'selected')),
  reason text,
  updated_by uuid,
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, updated_by) REFERENCES company.members(org_id, id)
);

-- Usage billing must carry an administrator approval and a positive cap.
CREATE FUNCTION company.c16_validate_model() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE approver_role text;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.provider IS DISTINCT FROM OLD.provider
       OR NEW.model_name IS DISTINCT FROM OLD.model_name
       OR NEW.tier IS DISTINCT FROM OLD.tier
       OR NEW.billing_mode IS DISTINCT FROM OLD.billing_mode
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
      RAISE EXCEPTION 'llm model identity and billing mode are immutable';
    END IF;
    IF OLD.state = 'revoked' AND NEW.state <> 'revoked' THEN
      RAISE EXCEPTION 'revoked llm model cannot be reactivated';
    END IF;
  END IF;
  IF NEW.billing_mode = 'usage' THEN
    IF NEW.monthly_cap_usd <= 0 OR NEW.billing_approved_at IS NULL
       OR NEW.billing_approved_by IS NULL THEN
      RAISE EXCEPTION 'usage billing requires approval and a positive cap';
    END IF;
    SELECT m.role INTO approver_role FROM company.members m
      WHERE m.org_id = NEW.org_id AND m.id = NEW.billing_approved_by
        AND m.state = 'active';
    IF approver_role IS DISTINCT FROM 'admin' THEN
      RAISE EXCEPTION 'usage billing requires an active administrator approval';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER llm_models_c16_validate BEFORE INSERT OR UPDATE ON company.llm_models
  FOR EACH ROW EXECUTE FUNCTION company.c16_validate_model();

-- Premium routes always need a standard fallback and cannot be active while
-- G13 is SKIP. Direct SQL cannot bypass either rule.
CREATE FUNCTION company.c16_validate_route() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE primary_model company.llm_models%ROWTYPE;
DECLARE fallback_model company.llm_models%ROWTYPE;
DECLARE g13 text;
BEGIN
  SELECT m.* INTO primary_model FROM company.llm_models m
    WHERE m.org_id = NEW.org_id AND m.id = NEW.primary_model_id AND m.state = 'active';
  IF NOT FOUND THEN RAISE EXCEPTION 'route primary model is unavailable'; END IF;
  IF primary_model.tier = 'premium' THEN
    SELECT COALESCE(s.g13_state, 'skip') INTO g13 FROM company.llm_settings s
      WHERE s.org_id = NEW.org_id;
    IF COALESCE(g13, 'skip') <> 'selected' THEN
      RAISE EXCEPTION 'premium route requires G13 selected';
    END IF;
    IF NEW.fallback_model_id IS NULL THEN
      RAISE EXCEPTION 'premium route requires a standard fallback';
    END IF;
  END IF;
  IF NEW.fallback_model_id IS NOT NULL THEN
    SELECT m.* INTO fallback_model FROM company.llm_models m
      WHERE m.org_id = NEW.org_id AND m.id = NEW.fallback_model_id AND m.state = 'active';
    IF NOT FOUND THEN RAISE EXCEPTION 'route fallback model is unavailable'; END IF;
    IF fallback_model.tier <> 'standard' THEN
      RAISE EXCEPTION 'route fallback must be a standard model';
    END IF;
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;
CREATE TRIGGER llm_routes_c16_validate BEFORE INSERT OR UPDATE ON company.llm_routes
  FOR EACH ROW EXECUTE FUNCTION company.c16_validate_route();

CREATE FUNCTION company.c16_identity_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.org_id IS DISTINCT FROM OLD.org_id THEN
    RAISE EXCEPTION 'llm row identity is immutable';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER llm_models_identity_immutable BEFORE UPDATE OF id, org_id ON company.llm_models
  FOR EACH ROW EXECUTE FUNCTION company.c16_identity_immutable();
CREATE TRIGGER llm_routes_identity_immutable BEFORE UPDATE OF id, org_id ON company.llm_routes
  FOR EACH ROW EXECUTE FUNCTION company.c16_identity_immutable();
CREATE TRIGGER llm_usage_identity_immutable BEFORE UPDATE OF id, org_id ON company.llm_usage
  FOR EACH ROW EXECUTE FUNCTION company.c16_identity_immutable();

-- Configuration changes are audited without credential material.
CREATE FUNCTION company.c16_audit_catalog() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actor uuid;
DECLARE action_name text;
DECLARE metadata jsonb;
BEGIN
  actor := NULLIF(current_setting('company.member_id', true), '')::uuid;
  IF TG_TABLE_NAME = 'llm_models' THEN
    action_name := 'model_configured';
    metadata := jsonb_build_object('provider', NEW.provider, 'model', NEW.model_name,
      'tier', NEW.tier, 'billing_mode', NEW.billing_mode,
      'monthly_cap_usd', NEW.monthly_cap_usd, 'state', NEW.state);
  ELSIF TG_TABLE_NAME = 'llm_routes' THEN
    action_name := 'route_configured';
    metadata := jsonb_build_object('task', NEW.task,
      'primary_model_id', NEW.primary_model_id,
      'fallback_model_id', NEW.fallback_model_id);
  ELSE
    action_name := 'g13_state_changed';
    metadata := jsonb_build_object('g13_state', NEW.g13_state, 'reason', NEW.reason);
  END IF;
  INSERT INTO company.audit_events(org_id, actor_id, event_type, action,
    resource_ref, decision, request_id, metadata)
  VALUES (NEW.org_id, actor, 'llm_routing', action_name,
    COALESCE(to_jsonb(NEW)->>'id', NEW.org_id::text), 'allow',
    gen_random_uuid()::text, metadata);
  RETURN NEW;
END;
$$;
CREATE TRIGGER llm_models_c16_audit AFTER INSERT OR UPDATE ON company.llm_models
  FOR EACH ROW EXECUTE FUNCTION company.c16_audit_catalog();
CREATE TRIGGER llm_routes_c16_audit AFTER INSERT OR UPDATE ON company.llm_routes
  FOR EACH ROW EXECUTE FUNCTION company.c16_audit_catalog();
CREATE TRIGGER llm_settings_c16_audit AFTER INSERT OR UPDATE ON company.llm_settings
  FOR EACH ROW EXECUTE FUNCTION company.c16_audit_catalog();

ALTER TABLE company.llm_models ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.llm_routes ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.llm_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.llm_budget_counters ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.llm_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.llm_models FORCE ROW LEVEL SECURITY;
ALTER TABLE company.llm_routes FORCE ROW LEVEL SECURITY;
ALTER TABLE company.llm_usage FORCE ROW LEVEL SECURITY;
ALTER TABLE company.llm_budget_counters FORCE ROW LEVEL SECURITY;
ALTER TABLE company.llm_settings FORCE ROW LEVEL SECURITY;

CREATE POLICY llm_model_read ON company.llm_models FOR SELECT USING (
  org_id::text = current_setting('company.org_id', true) AND company.c04_member_ready());
CREATE POLICY llm_model_insert ON company.llm_models FOR INSERT WITH CHECK (
  org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready());
CREATE POLICY llm_model_update ON company.llm_models FOR UPDATE USING (
  org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready())
  WITH CHECK (org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready());
CREATE POLICY llm_route_read ON company.llm_routes FOR SELECT USING (
  org_id::text = current_setting('company.org_id', true) AND company.c04_member_ready());
CREATE POLICY llm_route_insert ON company.llm_routes FOR INSERT WITH CHECK (
  org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready());
CREATE POLICY llm_route_update ON company.llm_routes FOR UPDATE USING (
  org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready())
  WITH CHECK (org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready());
CREATE POLICY llm_usage_read ON company.llm_usage FOR SELECT USING (
  org_id::text = current_setting('company.org_id', true)
  AND (company.c03_admin_ready()
       OR owner_id::text = current_setting('company.member_id', true)));
CREATE POLICY llm_usage_insert ON company.llm_usage FOR INSERT WITH CHECK (
  org_id::text = current_setting('company.org_id', true)
  AND owner_id::text = current_setting('company.member_id', true)
  AND actor_id::text = current_setting('company.member_id', true)
  AND company.c04_member_ready());
CREATE POLICY llm_counter_read ON company.llm_budget_counters FOR SELECT USING (
  org_id::text = current_setting('company.org_id', true) AND company.c04_member_ready());
CREATE POLICY llm_counter_insert ON company.llm_budget_counters FOR INSERT WITH CHECK (
  org_id::text = current_setting('company.org_id', true) AND company.c04_member_ready());
CREATE POLICY llm_counter_update ON company.llm_budget_counters FOR UPDATE USING (
  org_id::text = current_setting('company.org_id', true) AND company.c04_member_ready())
  WITH CHECK (org_id::text = current_setting('company.org_id', true) AND company.c04_member_ready());
CREATE POLICY llm_settings_read ON company.llm_settings FOR SELECT USING (
  org_id::text = current_setting('company.org_id', true) AND company.c04_member_ready());
CREATE POLICY llm_settings_insert ON company.llm_settings FOR INSERT WITH CHECK (
  org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready());
CREATE POLICY llm_settings_update ON company.llm_settings FOR UPDATE USING (
  org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready())
  WITH CHECK (org_id::text = current_setting('company.org_id', true) AND company.c03_admin_ready());

CREATE FUNCTION company.configure_llm_model(p_provider text, p_model_name text,
  p_tier text, p_billing_mode text, p_monthly_cap_usd numeric,
  p_billing_approved boolean)
RETURNS TABLE(decision text, model_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE existing company.llm_models%ROWTYPE;
DECLARE new_model company.llm_models%ROWTYPE;
DECLARE org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
DECLARE member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
BEGIN
  IF NOT company.c03_admin_ready()
     OR p_provider IS NULL OR p_provider = ''
     OR p_model_name IS NULL OR p_model_name = ''
     OR p_tier NOT IN ('standard', 'premium')
     OR p_billing_mode NOT IN ('flat', 'usage') THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  IF p_billing_mode = 'usage'
     AND (NOT p_billing_approved OR p_monthly_cap_usd IS NULL OR p_monthly_cap_usd <= 0) THEN
    RETURN QUERY SELECT 'approval_required'::text, NULL::uuid; RETURN;
  END IF;
  SELECT m.* INTO existing FROM company.llm_models m
    WHERE m.org_id = org AND m.provider = p_provider
      AND m.model_name = p_model_name AND m.state = 'active'
    FOR UPDATE;
  IF FOUND THEN
    IF existing.tier <> p_tier OR existing.billing_mode <> p_billing_mode THEN
      RETURN QUERY SELECT 'duplicate'::text, NULL::uuid; RETURN;
    END IF;
    UPDATE company.llm_models m
      SET monthly_cap_usd = COALESCE(p_monthly_cap_usd, 0),
          billing_approved_at = CASE WHEN p_billing_mode = 'usage' THEN now() ELSE NULL END,
          billing_approved_by = CASE WHEN p_billing_mode = 'usage' THEN member ELSE NULL END
      WHERE m.id = existing.id
      RETURNING m.* INTO new_model;
    RETURN QUERY SELECT 'configured'::text, new_model.id;
    RETURN;
  END IF;
  INSERT INTO company.llm_models(org_id, provider, model_name, tier, billing_mode,
    monthly_cap_usd, billing_approved_at, billing_approved_by)
  VALUES (org, p_provider, p_model_name, p_tier, p_billing_mode,
    COALESCE(p_monthly_cap_usd, 0),
    CASE WHEN p_billing_mode = 'usage' THEN now() ELSE NULL END,
    CASE WHEN p_billing_mode = 'usage' THEN member ELSE NULL END)
  RETURNING * INTO new_model;
  RETURN QUERY SELECT 'configured'::text, new_model.id;
END;
$$;

CREATE FUNCTION company.set_llm_route(p_task text, p_primary_model uuid,
  p_fallback_model uuid)
RETURNS TABLE(decision text, route_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE primary_model company.llm_models%ROWTYPE;
DECLARE fallback_model company.llm_models%ROWTYPE;
DECLARE g13 text;
DECLARE new_route company.llm_routes%ROWTYPE;
DECLARE member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
BEGIN
  IF NOT company.c03_admin_ready() OR p_task IS NULL OR p_task = '' THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  SELECT m.* INTO primary_model FROM company.llm_models m
    WHERE m.org_id::text = current_setting('company.org_id', true)
      AND m.id = p_primary_model AND m.state = 'active';
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'invalid_primary'::text, NULL::uuid; RETURN;
  END IF;
  IF primary_model.tier = 'premium' THEN
    SELECT COALESCE(s.g13_state, 'skip') INTO g13 FROM company.llm_settings s
      WHERE s.org_id = primary_model.org_id;
    IF COALESCE(g13, 'skip') <> 'selected' THEN
      RETURN QUERY SELECT 'g13_not_selected'::text, NULL::uuid; RETURN;
    END IF;
    IF p_fallback_model IS NULL THEN
      RETURN QUERY SELECT 'invalid_fallback'::text, NULL::uuid; RETURN;
    END IF;
  END IF;
  IF p_fallback_model IS NOT NULL THEN
    SELECT m.* INTO fallback_model FROM company.llm_models m
      WHERE m.org_id::text = current_setting('company.org_id', true)
        AND m.id = p_fallback_model AND m.state = 'active';
    IF NOT FOUND OR fallback_model.tier <> 'standard' THEN
      RETURN QUERY SELECT 'invalid_fallback'::text, NULL::uuid; RETURN;
    END IF;
  END IF;
  INSERT INTO company.llm_routes(org_id, task, primary_model_id, fallback_model_id, updated_by)
  VALUES (current_setting('company.org_id', true)::uuid, p_task,
    p_primary_model, p_fallback_model, member)
  ON CONFLICT (org_id, task) DO UPDATE
    SET primary_model_id = EXCLUDED.primary_model_id,
        fallback_model_id = EXCLUDED.fallback_model_id,
        updated_by = EXCLUDED.updated_by,
        updated_at = now()
  RETURNING * INTO new_route;
  RETURN QUERY SELECT 'configured'::text, new_route.id;
END;
$$;

CREATE FUNCTION company.set_llm_g13_state(p_state text, p_reason text)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
BEGIN
  IF NOT company.c03_admin_ready() OR p_state NOT IN ('skip', 'selected') THEN
    RETURN 'denied';
  END IF;
  INSERT INTO company.llm_settings(org_id, g13_state, reason, updated_by)
  VALUES (current_setting('company.org_id', true)::uuid, p_state, p_reason, member)
  ON CONFLICT (org_id) DO UPDATE
    SET g13_state = EXCLUDED.g13_state,
        reason = EXCLUDED.reason,
        updated_by = EXCLUDED.updated_by,
        updated_at = now();
  RETURN 'configured';
END;
$$;

CREATE FUNCTION company.llm_model_use_decision(p_model uuid)
RETURNS TABLE(decision text, reason_code text)
LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
DECLARE model company.llm_models%ROWTYPE;
DECLARE spend numeric(14,4);
BEGIN
  IF NOT company.c04_member_ready() THEN
    RETURN QUERY SELECT 'denied'::text, 'member_inactive'::text; RETURN;
  END IF;
  SELECT m.* INTO model FROM company.llm_models m
    WHERE m.org_id::text = current_setting('company.org_id', true)
      AND m.id = p_model AND m.state = 'active';
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'denied'::text, 'model_unavailable'::text; RETURN;
  END IF;
  IF model.billing_mode = 'flat' THEN
    RETURN QUERY SELECT 'allow'::text, 'flat_billing'::text; RETURN;
  END IF;
  IF model.billing_approved_at IS NULL OR model.billing_approved_by IS NULL
     OR model.monthly_cap_usd <= 0 THEN
    RETURN QUERY SELECT 'denied'::text, 'approval_required'::text; RETURN;
  END IF;
  SELECT c.spend_usd INTO spend FROM company.llm_budget_counters c
    WHERE c.org_id = model.org_id AND c.model_id = model.id
      AND c.month_key = to_char(now() AT TIME ZONE 'utc', 'YYYY-MM');
  IF COALESCE(spend, 0) >= model.monthly_cap_usd THEN
    RETURN QUERY SELECT 'denied'::text, 'cap_exceeded'::text; RETURN;
  END IF;
  RETURN QUERY SELECT 'allow'::text, 'usage_approved'::text;
END;
$$;

CREATE FUNCTION company.llm_route_decision(p_task text)
RETURNS TABLE(decision text, reason_code text,
  selected_model_id uuid, selected_provider text, selected_model text, selected_tier text,
  fallback_model_id uuid, fallback_provider text, fallback_model text, fallback_tier text)
LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
DECLARE route company.llm_routes%ROWTYPE;
DECLARE primary_model company.llm_models%ROWTYPE;
DECLARE fallback_model company.llm_models%ROWTYPE;
DECLARE primary_decision text;
DECLARE primary_reason text;
DECLARE fallback_decision text;
DECLARE fallback_reason text;
DECLARE g13 text;
BEGIN
  IF NOT company.c04_member_ready() THEN
    RETURN QUERY SELECT 'denied'::text, 'member_inactive'::text,
      NULL::uuid, NULL::text, NULL::text, NULL::text,
      NULL::uuid, NULL::text, NULL::text, NULL::text; RETURN;
  END IF;
  SELECT r.* INTO route FROM company.llm_routes r
    WHERE r.org_id::text = current_setting('company.org_id', true)
      AND r.task = p_task;
  IF NOT FOUND THEN
    SELECT r.* INTO route FROM company.llm_routes r
      WHERE r.org_id::text = current_setting('company.org_id', true)
        AND r.task = 'default';
  END IF;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'denied'::text, 'route_missing'::text,
      NULL::uuid, NULL::text, NULL::text, NULL::text,
      NULL::uuid, NULL::text, NULL::text, NULL::text; RETURN;
  END IF;
  SELECT m.* INTO primary_model FROM company.llm_models m
    WHERE m.org_id = route.org_id AND m.id = route.primary_model_id;
  SELECT COALESCE(s.g13_state, 'skip') INTO g13 FROM company.llm_settings s
    WHERE s.org_id = route.org_id;
  IF primary_model.tier = 'premium' AND COALESCE(g13, 'skip') <> 'selected' THEN
    primary_decision := 'denied';
    primary_reason := 'g13_skip';
  ELSE
    SELECT d.decision, d.reason_code INTO primary_decision, primary_reason
      FROM company.llm_model_use_decision(route.primary_model_id) d;
  END IF;
  IF primary_decision = 'allow' THEN
    IF route.fallback_model_id IS NOT NULL THEN
      SELECT m.* INTO fallback_model FROM company.llm_models m
        WHERE m.org_id = route.org_id AND m.id = route.fallback_model_id;
    END IF;
    RETURN QUERY SELECT 'allow'::text, 'primary_available'::text,
      primary_model.id, primary_model.provider, primary_model.model_name, primary_model.tier,
      fallback_model.id, fallback_model.provider, fallback_model.model_name, fallback_model.tier;
    RETURN;
  END IF;
  IF route.fallback_model_id IS NULL THEN
    RETURN QUERY SELECT 'denied'::text, primary_reason,
      NULL::uuid, NULL::text, NULL::text, NULL::text,
      NULL::uuid, NULL::text, NULL::text, NULL::text; RETURN;
  END IF;
  SELECT m.* INTO fallback_model FROM company.llm_models m
    WHERE m.org_id = route.org_id AND m.id = route.fallback_model_id;
  SELECT d.decision, d.reason_code INTO fallback_decision, fallback_reason
    FROM company.llm_model_use_decision(route.fallback_model_id) d;
  IF fallback_decision <> 'allow' THEN
    RETURN QUERY SELECT 'denied'::text, fallback_reason,
      NULL::uuid, NULL::text, NULL::text, NULL::text,
      NULL::uuid, NULL::text, NULL::text, NULL::text; RETURN;
  END IF;
  RETURN QUERY SELECT 'fallback'::text, primary_reason,
    fallback_model.id, fallback_model.provider, fallback_model.model_name, fallback_model.tier,
    NULL::uuid, NULL::text, NULL::text, NULL::text;
END;
$$;

CREATE FUNCTION company.record_llm_usage(p_model uuid, p_task text, p_outcome text,
  p_units integer, p_cost_usd numeric, p_request_id text)
RETURNS TABLE(decision text, usage_id uuid, month_spend_usd numeric, cap_usd numeric)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE model company.llm_models%ROWTYPE;
DECLARE spend numeric(14,4);
DECLARE new_usage company.llm_usage%ROWTYPE;
DECLARE org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
DECLARE member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
BEGIN
  IF NOT company.c04_member_ready()
     OR p_task IS NULL OR p_task = ''
     OR p_outcome NOT IN ('success', 'failure', 'fallback', 'blocked')
     OR p_units IS NULL OR p_units < 0
     OR p_cost_usd IS NULL OR p_cost_usd < 0
     OR p_request_id IS NULL OR p_request_id = '' THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::numeric, NULL::numeric; RETURN;
  END IF;
  SELECT m.* INTO model FROM company.llm_models m
    WHERE m.org_id = org AND m.id = p_model AND m.state = 'active';
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::numeric, NULL::numeric; RETURN;
  END IF;
  -- Advisory locks serialize cap checks without requiring model UPDATE rights.
  PERFORM pg_advisory_xact_lock(hashtextextended('c16-usage:' || model.id::text, 0));
  spend := 0;
  IF model.billing_mode = 'usage' THEN
    IF model.billing_approved_at IS NULL OR model.monthly_cap_usd <= 0 THEN
      INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
        resource_ref, decision, reason_code, request_id, metadata)
      VALUES (org, member, member, 'llm_usage', 'blocked', model.id::text,
        'deny', 'approval_required', p_request_id,
        jsonb_build_object('task', p_task, 'cost_usd', p_cost_usd));
      RETURN QUERY SELECT 'approval_required'::text, NULL::uuid, 0::numeric, model.monthly_cap_usd;
      RETURN;
    END IF;
    INSERT INTO company.llm_budget_counters(org_id, model_id, month_key, spend_usd)
    VALUES (org, model.id, to_char(now() AT TIME ZONE 'utc', 'YYYY-MM'), 0)
    ON CONFLICT (org_id, model_id, month_key) DO NOTHING;
    SELECT c.spend_usd INTO spend FROM company.llm_budget_counters c
      WHERE c.org_id = org AND c.model_id = model.id
        AND c.month_key = to_char(now() AT TIME ZONE 'utc', 'YYYY-MM')
      FOR UPDATE;
    spend := COALESCE(spend, 0);
    IF spend + p_cost_usd > model.monthly_cap_usd THEN
      INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
        resource_ref, decision, reason_code, request_id, metadata)
      VALUES (org, member, member, 'llm_usage', 'blocked', model.id::text,
        'deny', 'cap_exceeded', p_request_id,
        jsonb_build_object('task', p_task, 'cost_usd', p_cost_usd,
          'month_spend_usd', spend, 'monthly_cap_usd', model.monthly_cap_usd));
      RETURN QUERY SELECT 'cap_exceeded'::text, NULL::uuid, spend, model.monthly_cap_usd;
      RETURN;
    END IF;
    UPDATE company.llm_budget_counters c SET spend_usd = spend + p_cost_usd
      WHERE c.org_id = org AND c.model_id = model.id
        AND c.month_key = to_char(now() AT TIME ZONE 'utc', 'YYYY-MM');
  END IF;
  INSERT INTO company.llm_usage(org_id, owner_id, actor_id, model_id, task,
    outcome, units, cost_usd, request_id)
  VALUES (org, member, member, model.id, p_task, p_outcome,
    p_units, p_cost_usd, p_request_id)
  RETURNING * INTO new_usage;
  INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
    resource_ref, decision, request_id, metadata)
  VALUES (org, member, member, 'llm_usage', p_outcome, model.id::text,
    'allow', p_request_id,
    jsonb_build_object('task', p_task, 'cost_usd', p_cost_usd,
      'units', p_units, 'month_spend_usd', spend + p_cost_usd));
  RETURN QUERY SELECT 'recorded'::text, new_usage.id,
    spend + p_cost_usd, model.monthly_cap_usd;
END;
$$;
