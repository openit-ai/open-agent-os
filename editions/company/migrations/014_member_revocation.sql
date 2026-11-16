-- oaos-applied-with: 4
-- Member revocation on every suspension or departure (audit P1-4). Applied by
-- C05 (credentials exist from version 4) and every later phase.
--
-- Before this amendment only a transition to 'departed' revoked anything, and
-- only external accounts; C17's procedure revoked assistants and credentials
-- only while the member was still active. A suspended member therefore kept
-- active credential envelopes, and a suspended-then-departed member kept them
-- forever.
--
-- The owner-scoped C04/C05 policies only let the owner revoke their own rows
-- while active. A BEFORE UPDATE trigger on members still sees the old row, so
-- for an active member it impersonates that owner transaction-locally, revokes
-- assistants (whose C05 trigger revokes and clears every credential envelope),
-- then restores the caller context. Every later state is already empty of
-- active capabilities because this runs on the first transition away from
-- active. Sessions, delegations and external accounts are member-scoped
-- surfaces the member context may revoke in any state.

CREATE FUNCTION company.c05_revoke_member_capabilities() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  caller_org text := current_setting('company.org_id', true);
  caller_member text := current_setting('company.member_id', true);
BEGIN
  IF NEW.state IS DISTINCT FROM OLD.state THEN
    -- The 30-day personal-content deletion (migration 018) counts from here.
    NEW.updated_at := now();
  END IF;
  IF NEW.state NOT IN ('suspended', 'departed') OR NEW.state = OLD.state THEN
    RETURN NEW;
  END IF;
  PERFORM set_config('company.org_id', NEW.org_id::text, true);
  PERFORM set_config('company.member_id', NEW.id::text, true);
  IF OLD.state = 'active' THEN
    UPDATE company.assistants a SET state = 'revoked'
      WHERE a.org_id = NEW.org_id AND a.owner_id = NEW.id AND a.state = 'active';
  END IF;
  UPDATE company.external_accounts e SET state = 'revoked'
    WHERE e.org_id = NEW.org_id AND e.member_id = NEW.id AND e.state <> 'revoked';
  IF to_regclass('company.integration_delegations') IS NOT NULL THEN
    UPDATE company.integration_delegations d SET state = 'revoked', updated_at = now()
      WHERE d.org_id = NEW.org_id AND d.member_id = NEW.id AND d.state = 'active';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_attribute
              WHERE attrelid = to_regclass('company.console_sessions')
                AND attname = 'revoked_at' AND NOT attisdropped) THEN
    EXECUTE 'UPDATE company.console_sessions s SET state = ''revoked'', revoked_at = now()
              WHERE s.org_id = $1 AND s.member_id = $2 AND s.revoked_at IS NULL'
      USING NEW.org_id, NEW.id;
  ELSIF to_regclass('company.console_sessions') IS NOT NULL THEN
    EXECUTE 'UPDATE company.console_sessions s SET state = ''revoked''
              WHERE s.org_id = $1 AND s.member_id = $2 AND s.state = ''active'''
      USING NEW.org_id, NEW.id;
  END IF;
  PERFORM set_config('company.org_id', COALESCE(caller_org, ''), true);
  PERFORM set_config('company.member_id', COALESCE(caller_member, ''), true);
  RETURN NEW;
END;
$$;
CREATE TRIGGER members_c05_revoke_capabilities BEFORE UPDATE OF state ON company.members
  FOR EACH ROW EXECUTE FUNCTION company.c05_revoke_member_capabilities();

-- One-time repair for an E installed before this amendment: members who are
-- already suspended or departed lose any credential envelope or assistant that
-- is still active. Their owner-scoped policies no longer match an inactive
-- owner, so the owner role lifts FORCE RLS for this transaction only; triggers
-- still clear the envelope and write the audit row under the owner's identity.
ALTER TABLE company.assistants NO FORCE ROW LEVEL SECURITY;
ALTER TABLE company.secrets NO FORCE ROW LEVEL SECURITY;
ALTER TABLE company.audit_events NO FORCE ROW LEVEL SECURITY;
DO $repair$
DECLARE target record;
BEGIN
  FOR target IN
    SELECT m.org_id, m.id FROM company.members m
     WHERE m.state IN ('suspended', 'departed')
       AND (EXISTS (SELECT 1 FROM company.assistants a
                     WHERE a.org_id = m.org_id AND a.owner_id = m.id AND a.state = 'active')
            OR EXISTS (SELECT 1 FROM company.secrets s
                        WHERE s.org_id = m.org_id AND s.owner_id = m.id AND s.state = 'active'))
  LOOP
    PERFORM set_config('company.org_id', target.org_id::text, true);
    PERFORM set_config('company.member_id', target.id::text, true);
    UPDATE company.secrets s SET state = 'revoked'
      WHERE s.org_id = target.org_id AND s.owner_id = target.id AND s.state = 'active';
    UPDATE company.assistants a SET state = 'revoked'
      WHERE a.org_id = target.org_id AND a.owner_id = target.id AND a.state = 'active';
  END LOOP;
  PERFORM set_config('company.org_id', '', true);
  PERFORM set_config('company.member_id', '', true);
END;
$repair$;
ALTER TABLE company.assistants FORCE ROW LEVEL SECURITY;
ALTER TABLE company.secrets FORCE ROW LEVEL SECURITY;
ALTER TABLE company.audit_events FORCE ROW LEVEL SECURITY;
