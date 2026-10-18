-- C16 real SQL responses. Fixtures and their audit rows are discarded with this
-- transaction; the persisted default route and G13 state are only read.
BEGIN;
SELECT set_config('c16.persisted_org', :'org', true);
SELECT set_config('c16.persisted_admin', :'admin', true);
DO $verify$
DECLARE
  persisted_org uuid := current_setting('c16.persisted_org')::uuid;
  persisted_admin uuid := current_setting('c16.persisted_admin')::uuid;
  org_a uuid := gen_random_uuid();
  org_b uuid := gen_random_uuid();
  admin_a uuid := gen_random_uuid();
  owner_a uuid := gen_random_uuid();
  owner_b uuid := gen_random_uuid();
  admin_b uuid := gen_random_uuid();
  standard_a uuid;
  premium_a uuid;
  premium_flat uuid;
  standard_b uuid;
  decision text;
  reason text;
  selected uuid;
  state text;
  spend numeric;
  cap numeric;
  usage_id uuid;
  rows integer;
  rejected boolean;
BEGIN
  -- Persisted configuration: exactly one standard default route, and a selected
  -- G13 state only when an approved premium usage model has a standard fallback.
  PERFORM set_config('company.org_id', persisted_org::text, true);
  PERFORM set_config('company.member_id', persisted_admin::text, true);
  SELECT s.g13_state INTO state FROM company.llm_settings s WHERE s.org_id = persisted_org;
  IF state NOT IN ('skip', 'selected') THEN
    RAISE EXCEPTION 'persisted G13 state is missing';
  END IF;
  SELECT count(*) INTO rows FROM company.llm_routes r
    JOIN company.llm_models m ON m.org_id = r.org_id AND m.id = r.primary_model_id
    WHERE r.org_id = persisted_org AND r.task = 'default'
      AND m.tier = 'standard' AND m.state = 'active';
  IF rows <> 1 THEN RAISE EXCEPTION 'default standard route is missing'; END IF;
  IF state = 'selected' THEN
    IF NOT EXISTS (
      SELECT 1 FROM company.llm_models m
      WHERE m.org_id = persisted_org AND m.tier = 'premium' AND m.state = 'active'
        AND m.billing_approved_at IS NOT NULL AND m.monthly_cap_usd > 0
    ) THEN RAISE EXCEPTION 'selected G13 has no approved premium model'; END IF;
    IF NOT EXISTS (
      SELECT 1 FROM company.llm_routes r
      JOIN company.llm_models p ON p.org_id = r.org_id AND p.id = r.primary_model_id
      JOIN company.llm_models f ON f.org_id = r.org_id AND f.id = r.fallback_model_id
      WHERE r.org_id = persisted_org AND p.tier = 'premium' AND f.tier = 'standard'
    ) THEN RAISE EXCEPTION 'selected G13 has no premium route with standard fallback'; END IF;
  END IF;

  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_a, 'c16-a-' || org_a::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (admin_a, org_a, 'c16-admin-' || admin_a::text, 'admin', 'active'),
    (owner_a, org_a, 'c16-owner-' || owner_a::text, 'member', 'active'),
    (owner_b, org_a, 'c16-owner-' || owner_b::text, 'member', 'active');
  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', admin_b::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_b, 'c16-b-' || org_b::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (admin_b, org_b, 'c16-admin-' || admin_b::text, 'admin', 'active');
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);

  SELECT r.model_id INTO standard_a FROM company.configure_llm_model(
    'c16-standard', 'standard-model', 'standard', 'flat', 0, false) r;
  IF standard_a IS NULL THEN RAISE EXCEPTION 'standard model configuration failed'; END IF;
  SELECT r.decision INTO decision FROM company.configure_llm_model(
    'c16-premium', 'premium-model', 'premium', 'usage', 5, false) r;
  IF decision IS DISTINCT FROM 'approval_required' THEN
    RAISE EXCEPTION 'unapproved usage billing was accepted';
  END IF;
  IF EXISTS (SELECT 1 FROM company.llm_models WHERE org_id = org_a AND provider = 'c16-premium') THEN
    RAISE EXCEPTION 'unapproved usage model was stored';
  END IF;
  SELECT r.model_id INTO premium_a FROM company.configure_llm_model(
    'c16-premium', 'premium-model', 'premium', 'usage', 5, true) r;
  IF premium_a IS NULL THEN RAISE EXCEPTION 'approved premium model configuration failed'; END IF;
  IF (SELECT company.set_llm_g13_state('selected', 'fixture') ) IS DISTINCT FROM 'configured' THEN
    RAISE EXCEPTION 'G13 selected state failed';
  END IF;
  IF (SELECT r.decision FROM company.set_llm_route('default', standard_a, NULL) r)
     IS DISTINCT FROM 'configured' THEN
    RAISE EXCEPTION 'default route configuration failed';
  END IF;
  IF (SELECT r.decision FROM company.set_llm_route('complex', premium_a, standard_a) r)
     IS DISTINCT FROM 'configured' THEN
    RAISE EXCEPTION 'premium route configuration failed';
  END IF;

  SELECT d.decision, d.reason_code, d.selected_model_id
    INTO decision, reason, selected FROM company.llm_route_decision('complex') d;
  IF decision IS DISTINCT FROM 'allow' OR selected IS DISTINCT FROM premium_a THEN
    RAISE EXCEPTION 'premium task routing failed: % / %', decision, reason;
  END IF;
  SELECT d.decision, d.selected_model_id INTO decision, selected
    FROM company.llm_route_decision('unconfigured-task') d;
  IF decision IS DISTINCT FROM 'allow' OR selected IS DISTINCT FROM standard_a THEN
    RAISE EXCEPTION 'unknown task did not use the default standard route';
  END IF;

  PERFORM set_config('company.member_id', owner_a::text, true);
  SELECT r.decision, r.usage_id, r.month_spend_usd, r.cap_usd
    INTO decision, usage_id, spend, cap
    FROM company.record_llm_usage(premium_a, 'complex', 'success', 100, 4.5,
      'c16-request-a-1') r;
  IF decision IS DISTINCT FROM 'recorded' OR usage_id IS NULL OR spend <> 4.5 OR cap <> 5 THEN
    RAISE EXCEPTION 'usage record failed: % / % / %', decision, spend, cap;
  END IF;
  IF (SELECT d.decision FROM company.llm_model_use_decision(premium_a) d)
     IS DISTINCT FROM 'allow' THEN
    RAISE EXCEPTION 'approved model below cap was rejected';
  END IF;

  -- A second active member shares the organization policy but not usage rows.
  PERFORM set_config('company.member_id', owner_b::text, true);
  SELECT d.decision, d.selected_model_id INTO decision, selected
    FROM company.llm_route_decision('complex') d;
  IF decision IS DISTINCT FROM 'allow' OR selected IS DISTINCT FROM premium_a THEN
    RAISE EXCEPTION 'second owner route selection failed';
  END IF;
  SELECT r.decision INTO decision FROM company.record_llm_usage(
    premium_a, 'complex', 'success', 10, 0.4, 'c16-request-b-1') r;
  IF decision IS DISTINCT FROM 'recorded' THEN RAISE EXCEPTION 'second owner usage failed'; END IF;
  IF (SELECT count(*) FROM company.llm_usage) <> 1 THEN
    RAISE EXCEPTION 'second owner saw another owner usage row';
  END IF;
  PERFORM set_config('company.member_id', owner_a::text, true);
  IF (SELECT count(*) FROM company.llm_usage) <> 1 THEN
    RAISE EXCEPTION 'owner usage rows were not isolated';
  END IF;
  IF (SELECT count(*) FROM company.llm_usage WHERE owner_id = owner_b) <> 0 THEN
    RAISE EXCEPTION 'foreign owner usage was visible';
  END IF;
  PERFORM set_config('company.member_id', admin_a::text, true);
  IF (SELECT count(*) FROM company.llm_usage) <> 2 THEN
    RAISE EXCEPTION 'administrator did not see both usage rows';
  END IF;

  -- Reaching the cap forces the standard fallback and blocks further spend.
  PERFORM set_config('company.member_id', owner_a::text, true);
  SELECT r.decision, r.month_spend_usd INTO decision, spend
    FROM company.record_llm_usage(premium_a, 'complex', 'success', 100, 0.1,
      'c16-request-a-2') r;
  IF decision IS DISTINCT FROM 'recorded' OR spend <> 5 THEN
    RAISE EXCEPTION 'cap boundary usage failed: % / %', decision, spend;
  END IF;
  IF (SELECT d.decision FROM company.llm_model_use_decision(premium_a) d)
     IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'model at cap was still allowed';
  END IF;
  SELECT r.decision, r.usage_id, r.month_spend_usd INTO decision, usage_id, spend
    FROM company.record_llm_usage(premium_a, 'complex', 'success', 100, 0.1,
      'c16-request-a-3') r;
  IF decision IS DISTINCT FROM 'cap_exceeded' OR usage_id IS NOT NULL OR spend <> 5 THEN
    RAISE EXCEPTION 'monthly cap was not enforced: % / %', decision, spend;
  END IF;
  SELECT d.decision, d.reason_code, d.selected_model_id
    INTO decision, reason, selected FROM company.llm_route_decision('complex') d;
  IF decision IS DISTINCT FROM 'fallback' OR reason IS DISTINCT FROM 'cap_exceeded'
     OR selected IS DISTINCT FROM standard_a THEN
    RAISE EXCEPTION 'cap fallback failed: % / %', decision, reason;
  END IF;
  PERFORM set_config('company.member_id', admin_a::text, true);
  IF (SELECT company.set_llm_g13_state('skip', 'fixture_skip')) IS DISTINCT FROM 'configured' THEN
    RAISE EXCEPTION 'G13 SKIP state failed';
  END IF;
  SELECT d.decision, d.reason_code, d.selected_model_id
    INTO decision, reason, selected FROM company.llm_route_decision('complex') d;
  IF decision IS DISTINCT FROM 'fallback' OR reason IS DISTINCT FROM 'g13_skip'
     OR selected IS DISTINCT FROM standard_a THEN
    RAISE EXCEPTION 'G13 SKIP did not fall back: % / %', decision, reason;
  END IF;
  IF (SELECT company.set_llm_g13_state('selected', 'fixture')) IS DISTINCT FROM 'configured' THEN
    RAISE EXCEPTION 'G13 selected restore failed';
  END IF;

  -- Members cannot change model policy; only an active administrator can.
  PERFORM set_config('company.member_id', owner_a::text, true);
  SELECT r.decision INTO decision FROM company.configure_llm_model(
    'c16-member', 'member-model', 'standard', 'flat', 0, false) r;
  IF decision IS DISTINCT FROM 'denied' THEN RAISE EXCEPTION 'member changed model policy'; END IF;
  SELECT r.decision INTO decision FROM company.set_llm_route('member', premium_a, standard_a) r;
  IF decision IS DISTINCT FROM 'denied' THEN RAISE EXCEPTION 'member changed routing'; END IF;

  -- Premium fallback must be a standard model.
  PERFORM set_config('company.member_id', admin_a::text, true);
  SELECT r.model_id INTO premium_flat FROM company.configure_llm_model(
    'c16-premium-flat', 'premium-flat', 'premium', 'flat', 0, false) r;
  SELECT r.decision INTO decision FROM company.set_llm_route('bad-fallback', premium_flat, premium_a) r;
  IF decision IS DISTINCT FROM 'invalid_fallback' THEN
    RAISE EXCEPTION 'premium fallback was accepted';
  END IF;

  -- Other organizations see only their own catalog and cannot write into A.
  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', admin_b::text, true);
  SELECT r.model_id INTO standard_b FROM company.configure_llm_model(
    'c16-standard-b', 'standard-b', 'standard', 'flat', 0, false) r;
  IF standard_b IS NULL THEN RAISE EXCEPTION 'second organization model failed'; END IF;
  IF (SELECT r.decision FROM company.set_llm_route('default', standard_b, NULL) r)
     IS DISTINCT FROM 'configured' THEN
    RAISE EXCEPTION 'second organization default route failed';
  END IF;
  IF (SELECT count(*) FROM company.llm_models) <> 1 THEN
    RAISE EXCEPTION 'cross-organization model catalog was visible';
  END IF;
  IF (SELECT d.decision FROM company.llm_route_decision('complex') d) IS DISTINCT FROM 'allow' THEN
    RAISE EXCEPTION 'second organization default route failed';
  END IF;
  rejected := false;
  BEGIN
    INSERT INTO company.llm_routes(org_id, task, primary_model_id, fallback_model_id)
      VALUES (org_a, 'cross-org', standard_a, NULL);
  EXCEPTION WHEN insufficient_privilege OR foreign_key_violation OR raise_exception THEN
    rejected := true;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'cross-organization route write was accepted'; END IF;

  -- Configuration and usage audit rows exist without credential material.
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  IF NOT EXISTS (SELECT 1 FROM company.audit_events
    WHERE org_id = org_a AND event_type = 'llm_routing' AND action = 'route_configured') THEN
    RAISE EXCEPTION 'routing audit event is missing';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM company.audit_events
    WHERE org_id = org_a AND event_type = 'llm_usage' AND action = 'blocked'
      AND reason_code = 'cap_exceeded') THEN
    RAISE EXCEPTION 'blocked usage audit event is missing';
  END IF;
  IF EXISTS (SELECT 1 FROM company.audit_events
    WHERE org_id = org_a AND event_type IN ('llm_routing', 'llm_usage')
      AND (metadata ? 'api_key' OR metadata ? 'secret' OR metadata ? 'token')) THEN
    RAISE EXCEPTION 'audit metadata contained credential material';
  END IF;
END;
$verify$;
ROLLBACK;
