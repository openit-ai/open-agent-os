-- C17 final validation and operational handover.
--
-- This migration records the operational evidence a Company administrator must
-- collect before handover: a verified database backup, a recovery drill, the
-- post-reboot service state and the external round-trip result for every
-- selected integration. It also defines the single departing-member revocation
-- procedure so assistants, credentials, delegations, console sessions and
-- external mappings are revoked in one transaction.
--
-- The trusted server sets company.org_id and company.member_id after it
-- authenticated the Project session. Only an active administrator may record
-- evidence or revoke a member. No credential material is stored in this schema.

CREATE TABLE company.backup_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  kind text NOT NULL CHECK (kind IN ('scheduled', 'manual', 'pre_handover')),
  location text NOT NULL CHECK (location <> '' AND length(location) <= 1024),
  checksum char(64) NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
  size_bytes bigint NOT NULL CHECK (size_bytes > 0),
  state text NOT NULL DEFAULT 'verified' CHECK (state IN ('verified', 'failed')),
  verified_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  recorded_by uuid,
  FOREIGN KEY (org_id, recorded_by) REFERENCES company.members(org_id, id),
  UNIQUE (org_id, id),
  CHECK (state <> 'verified' OR verified_at IS NOT NULL)
);

CREATE TABLE company.recovery_drills (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  backup_id uuid NOT NULL,
  target text NOT NULL CHECK (target <> '' AND length(target) <= 256),
  state text NOT NULL CHECK (state IN ('passed', 'failed')),
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) = 'object'),
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz NOT NULL DEFAULT now(),
  performed_by uuid,
  FOREIGN KEY (org_id, backup_id) REFERENCES company.backup_runs(org_id, id),
  FOREIGN KEY (org_id, performed_by) REFERENCES company.members(org_id, id),
  UNIQUE (org_id, id)
);

CREATE TABLE company.member_revocations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id uuid NOT NULL REFERENCES company.organizations(id),
  member_id uuid NOT NULL,
  reason text NOT NULL CHECK (reason <> '' AND length(reason) <= 256),
  sessions_revoked integer NOT NULL DEFAULT 0 CHECK (sessions_revoked >= 0),
  assistants_revoked integer NOT NULL DEFAULT 0 CHECK (assistants_revoked >= 0),
  credentials_revoked integer NOT NULL DEFAULT 0 CHECK (credentials_revoked >= 0),
  delegations_revoked integer NOT NULL DEFAULT 0 CHECK (delegations_revoked >= 0),
  accounts_revoked integer NOT NULL DEFAULT 0 CHECK (accounts_revoked >= 0),
  performed_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, member_id) REFERENCES company.members(org_id, id),
  FOREIGN KEY (org_id, performed_by) REFERENCES company.members(org_id, id),
  UNIQUE (org_id, member_id),
  UNIQUE (org_id, id)
);

CREATE TABLE company.handover_state (
  org_id uuid PRIMARY KEY REFERENCES company.organizations(id),
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'verified')),
  service_state text NOT NULL DEFAULT 'unknown',
  backup_id uuid,
  recovery_id uuid,
  boot_id text,
  previous_boot_id text,
  reboot_verified boolean NOT NULL DEFAULT false,
  external_roundtrip jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(external_roundtrip) = 'object'),
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(evidence) = 'object'),
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  recorded_by uuid,
  FOREIGN KEY (org_id, backup_id) REFERENCES company.backup_runs(org_id, id),
  FOREIGN KEY (org_id, recovery_id) REFERENCES company.recovery_drills(org_id, id),
  FOREIGN KEY (org_id, recorded_by) REFERENCES company.members(org_id, id),
  CHECK (state <> 'verified' OR (completed_at IS NOT NULL AND reboot_verified))
);

-- Assistants and credential envelopes stay owner-scoped; an administrator never
-- gets SELECT on ciphertext. Revocation therefore runs the owner-scoped updates
-- under the departing member's own context (see c17_revoke_member) so the C04/C05
-- owner policies and triggers still apply.

ALTER TABLE company.backup_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.recovery_drills ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.member_revocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.handover_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.backup_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE company.recovery_drills FORCE ROW LEVEL SECURITY;
ALTER TABLE company.member_revocations FORCE ROW LEVEL SECURITY;
ALTER TABLE company.handover_state FORCE ROW LEVEL SECURITY;

CREATE POLICY backup_runs_read ON company.backup_runs FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true)
         AND company.c04_member_ready());
CREATE POLICY backup_runs_insert ON company.backup_runs FOR INSERT
  WITH CHECK (org_id::text = current_setting('company.org_id', true)
              AND company.c03_admin_ready());
CREATE POLICY backup_runs_update ON company.backup_runs FOR UPDATE
  USING (org_id::text = current_setting('company.org_id', true)
         AND company.c03_admin_ready())
  WITH CHECK (org_id::text = current_setting('company.org_id', true)
              AND company.c03_admin_ready());

CREATE POLICY recovery_drills_read ON company.recovery_drills FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true)
         AND company.c04_member_ready());
CREATE POLICY recovery_drills_insert ON company.recovery_drills FOR INSERT
  WITH CHECK (org_id::text = current_setting('company.org_id', true)
              AND company.c03_admin_ready());
CREATE POLICY recovery_drills_update ON company.recovery_drills FOR UPDATE
  USING (org_id::text = current_setting('company.org_id', true)
         AND company.c03_admin_ready())
  WITH CHECK (org_id::text = current_setting('company.org_id', true)
              AND company.c03_admin_ready());

CREATE POLICY member_revocations_read ON company.member_revocations FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true)
         AND (company.c03_admin_ready()
              OR member_id::text = current_setting('company.member_id', true)));
CREATE POLICY member_revocations_insert ON company.member_revocations FOR INSERT
  WITH CHECK (org_id::text = current_setting('company.org_id', true)
              AND company.c03_admin_ready());

CREATE POLICY handover_state_read ON company.handover_state FOR SELECT
  USING (org_id::text = current_setting('company.org_id', true)
         AND company.c03_admin_ready());
CREATE POLICY handover_state_insert ON company.handover_state FOR INSERT
  WITH CHECK (org_id::text = current_setting('company.org_id', true)
              AND company.c03_admin_ready());
CREATE POLICY handover_state_update ON company.handover_state FOR UPDATE
  USING (org_id::text = current_setting('company.org_id', true)
         AND company.c03_admin_ready())
  WITH CHECK (org_id::text = current_setting('company.org_id', true)
              AND company.c03_admin_ready());

CREATE FUNCTION company.c17_evidence_safe(p_evidence jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT COALESCE(p_evidence, '{}'::jsonb) = '{}'::jsonb
    OR (jsonb_typeof(p_evidence) = 'object'
        AND NOT (p_evidence ?| ARRAY[
          'ciphertext', 'nonce', 'token', 'secret', 'password', 'key', 'body',
          'snippet', 'prompt', 'api_key', 'authorization', 'cookie', 'csrf']::text[]));
$$;

CREATE FUNCTION company.c17_record_backup(p_kind text, p_location text,
  p_checksum text, p_size_bytes bigint)
RETURNS TABLE(decision text, backup_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE
  org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
  member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
  created company.backup_runs%ROWTYPE;
BEGIN
  IF NOT company.c03_admin_ready() OR org IS NULL OR member IS NULL
     OR p_kind IS NULL OR p_kind NOT IN ('scheduled', 'manual', 'pre_handover')
     OR p_location IS NULL OR p_location = '' OR length(p_location) > 1024
     OR p_checksum IS NULL OR p_checksum !~ '^[0-9a-f]{64}$'
     OR p_size_bytes IS NULL OR p_size_bytes <= 0 THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid;
    RETURN;
  END IF;
  INSERT INTO company.backup_runs(org_id, kind, location, checksum, size_bytes,
    state, verified_at, recorded_by)
  VALUES (org, p_kind, p_location, p_checksum, p_size_bytes, 'verified', now(), member)
  RETURNING * INTO created;
  RETURN QUERY SELECT 'recorded'::text, created.id;
END;
$$;

CREATE FUNCTION company.c17_record_recovery(p_backup_id uuid, p_target text,
  p_passed boolean, p_evidence jsonb DEFAULT '{}'::jsonb)
RETURNS TABLE(decision text, recovery_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE
  org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
  member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
  backup_state text;
  created company.recovery_drills%ROWTYPE;
BEGIN
  IF NOT company.c03_admin_ready() OR org IS NULL OR member IS NULL
     OR p_backup_id IS NULL
     OR p_target IS NULL OR p_target = '' OR length(p_target) > 256
     OR p_passed IS NULL
     OR NOT company.c17_evidence_safe(p_evidence) THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid;
    RETURN;
  END IF;
  SELECT b.state INTO backup_state FROM company.backup_runs b
    WHERE b.id = p_backup_id AND b.org_id = org;
  IF NOT FOUND OR backup_state <> 'verified' THEN
    RETURN QUERY SELECT 'backup_unavailable'::text, NULL::uuid;
    RETURN;
  END IF;
  INSERT INTO company.recovery_drills(org_id, backup_id, target, state, evidence,
    completed_at, performed_by)
  VALUES (org, p_backup_id, p_target,
    CASE WHEN p_passed THEN 'passed' ELSE 'failed' END,
    COALESCE(p_evidence, '{}'::jsonb), now(), member)
  RETURNING * INTO created;
  RETURN QUERY SELECT 'recorded'::text, created.id;
END;
$$;

-- One handover verdict per organization. The row only becomes verified when the
-- latest verified backup has a passed recovery drill, the service is active, the
-- boot ID changed after a reboot, and every selected integration has a confirmed
-- external round-trip. Re-running with incomplete evidence preserves an earlier
-- verified verdict instead of downgrading it.
CREATE FUNCTION company.c17_record_handover(p_service_state text, p_boot_id text,
  p_previous_boot_id text, p_reboot_verified boolean,
  p_external_roundtrip jsonb, p_evidence jsonb DEFAULT '{}'::jsonb)
RETURNS TABLE(decision text, state text)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE
  org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
  member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
  latest_backup uuid;
  latest_recovery uuid;
  missing_provider text;
  ready boolean;
  result_state text;
BEGIN
  IF NOT company.c03_admin_ready() OR org IS NULL OR member IS NULL
     OR p_service_state IS NULL OR p_service_state = ''
     OR p_external_roundtrip IS NULL
     OR jsonb_typeof(p_external_roundtrip) <> 'object'
     OR NOT company.c17_evidence_safe(p_evidence) THEN
    RETURN QUERY SELECT 'denied'::text, 'pending'::text;
    RETURN;
  END IF;

  SELECT b.id INTO latest_backup FROM company.backup_runs b
    WHERE b.org_id = org AND b.state = 'verified'
    ORDER BY b.created_at DESC, b.id DESC LIMIT 1;
  IF latest_backup IS NOT NULL THEN
    SELECT r.id INTO latest_recovery FROM company.recovery_drills r
      WHERE r.org_id = org AND r.backup_id = latest_backup AND r.state = 'passed'
      ORDER BY r.completed_at DESC, r.id DESC LIMIT 1;
  END IF;
  SELECT s.provider INTO missing_provider FROM company.integration_settings s
    WHERE s.org_id = org AND s.state = 'selected'
      AND COALESCE(p_external_roundtrip->>s.provider, '') <> 'confirmed'
    ORDER BY s.provider LIMIT 1;

  ready := p_service_state = 'active'
    AND COALESCE(p_reboot_verified, false)
    AND p_boot_id IS NOT NULL AND p_boot_id <> ''
    AND p_previous_boot_id IS NOT NULL AND p_previous_boot_id <> ''
    AND p_boot_id <> p_previous_boot_id
    AND latest_backup IS NOT NULL AND latest_recovery IS NOT NULL
    AND missing_provider IS NULL;
  result_state := CASE WHEN ready THEN 'verified' ELSE 'pending' END;

  INSERT INTO company.handover_state(org_id, state, service_state, backup_id,
    recovery_id, boot_id, previous_boot_id, reboot_verified, external_roundtrip,
    evidence, completed_at, recorded_by)
  VALUES (org, result_state, p_service_state, latest_backup, latest_recovery,
    p_boot_id, p_previous_boot_id, COALESCE(p_reboot_verified, false),
    p_external_roundtrip, COALESCE(p_evidence, '{}'::jsonb),
    CASE WHEN ready THEN now() ELSE NULL END, member)
  ON CONFLICT (org_id) DO UPDATE SET
    state = CASE WHEN company.handover_state.state = 'verified'
                 THEN 'verified' ELSE EXCLUDED.state END,
    service_state = EXCLUDED.service_state,
    backup_id = COALESCE(EXCLUDED.backup_id, company.handover_state.backup_id),
    recovery_id = COALESCE(EXCLUDED.recovery_id, company.handover_state.recovery_id),
    boot_id = EXCLUDED.boot_id,
    previous_boot_id = EXCLUDED.previous_boot_id,
    reboot_verified = CASE WHEN company.handover_state.state = 'verified'
                           THEN true ELSE EXCLUDED.reboot_verified END,
    external_roundtrip = EXCLUDED.external_roundtrip,
    evidence = EXCLUDED.evidence,
    completed_at = CASE WHEN company.handover_state.state = 'verified'
                        THEN company.handover_state.completed_at
                        ELSE EXCLUDED.completed_at END,
    recorded_by = EXCLUDED.recorded_by
  RETURNING company.handover_state.state INTO result_state;

  RETURN QUERY SELECT result_state, result_state;
END;
$$;

-- Revoke every capability owned by a departing member in one transaction. The
-- member lifecycle trigger keeps the final state departed, so a later request
-- cannot reactivate the account. The last active administrator is refused before
-- any write happens.
CREATE FUNCTION company.c17_revoke_member(p_member_id uuid, p_reason text)
RETURNS TABLE(decision text, sessions_revoked integer, assistants_revoked integer,
  credentials_revoked integer, delegations_revoked integer, accounts_revoked integer)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE
  org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
  member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
  target company.members%ROWTYPE;
  other_admins integer;
  n_sessions integer := 0;
  n_assistants integer := 0;
  n_credentials integer := 0;
  n_delegations integer := 0;
  n_accounts integer := 0;
BEGIN
  IF NOT company.c03_admin_ready() OR org IS NULL OR member IS NULL
     OR p_member_id IS NULL
     OR p_reason IS NULL OR p_reason = '' OR length(p_reason) > 256 THEN
    RETURN QUERY SELECT 'denied'::text, 0, 0, 0, 0, 0;
    RETURN;
  END IF;
  SELECT m.* INTO target FROM company.members m
    WHERE m.org_id = org AND m.id = p_member_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'unregistered'::text, 0, 0, 0, 0, 0;
    RETURN;
  END IF;
  IF target.state = 'departed' THEN
    RETURN QUERY SELECT 'already_revoked'::text, 0, 0, 0, 0, 0;
    RETURN;
  END IF;
  IF target.role = 'admin' AND target.state = 'active' THEN
    SELECT count(*) INTO other_admins FROM company.members m
      WHERE m.org_id = org AND m.role = 'admin' AND m.state = 'active'
        AND m.id <> target.id;
    IF other_admins = 0 THEN
      RETURN QUERY SELECT 'last_admin'::text, 0, 0, 0, 0, 0;
      RETURN;
    END IF;
  END IF;

  -- Administrator-scoped surfaces: sessions, delegations and external accounts.
  UPDATE company.console_sessions s SET state = 'revoked', revoked_at = now()
    WHERE s.org_id = org AND s.member_id = target.id AND s.revoked_at IS NULL;
  GET DIAGNOSTICS n_sessions = ROW_COUNT;
  UPDATE company.integration_delegations d SET state = 'revoked', updated_at = now()
    WHERE d.org_id = org AND d.member_id = target.id AND d.state = 'active';
  GET DIAGNOSTICS n_delegations = ROW_COUNT;
  UPDATE company.external_accounts e SET state = 'revoked'
    WHERE e.org_id = org AND e.member_id = target.id AND e.state <> 'revoked';
  GET DIAGNOSTICS n_accounts = ROW_COUNT;

  -- Personal assistant and credential rows are owner-scoped, so run the C04/C05
  -- revocation under the departing member's own context. The administrator never
  -- reads a ciphertext; the C05 trigger clears the envelope on revocation.
  PERFORM set_config('company.member_id', target.id::text, true);
  IF company.c04_member_ready() THEN
    SELECT count(*) INTO n_credentials FROM company.secrets s
      WHERE s.org_id = org AND s.owner_id = target.id AND s.state = 'active';
    UPDATE company.assistants a SET state = 'revoked'
      WHERE a.org_id = org AND a.owner_id = target.id AND a.state <> 'revoked';
    GET DIAGNOSTICS n_assistants = ROW_COUNT;
    UPDATE company.secrets s SET state = 'revoked'
      WHERE s.org_id = org AND s.owner_id = target.id AND s.state = 'active';
  END IF;
  PERFORM set_config('company.member_id', member::text, true);

  UPDATE company.members m SET state = 'departed', updated_at = now()
    WHERE m.org_id = org AND m.id = target.id;

  INSERT INTO company.member_revocations(org_id, member_id, reason,
    sessions_revoked, assistants_revoked, credentials_revoked,
    delegations_revoked, accounts_revoked, performed_by)
  VALUES (org, target.id, p_reason, n_sessions, n_assistants, n_credentials,
    n_delegations, n_accounts, member);

  INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
    resource_ref, decision, reason_code, request_id, metadata)
  VALUES (org, member, target.id, 'handover', 'member_revoked', target.id::text,
    'allow', p_reason, gen_random_uuid()::text,
    jsonb_build_object('sessions', n_sessions, 'assistants', n_assistants,
      'credentials', n_credentials, 'delegations', n_delegations,
      'accounts', n_accounts));

  RETURN QUERY SELECT 'revoked'::text, n_sessions, n_assistants,
    n_credentials, n_delegations, n_accounts;
END;
$$;
