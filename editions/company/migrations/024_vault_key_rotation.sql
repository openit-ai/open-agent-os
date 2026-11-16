-- oaos-applied-with: 4
-- SPDX-License-Identifier: BUSL-1.1
-- Company edition: see LICENSE-COMPANY at the repository root.
--
-- Vault key rotation (company-design 10, O5). Applied by C05 and every later
-- phase through the maintenance path as the NOLOGIN Company owner role.
--
-- The vault master key lives only in the Company EnvironmentFile as
-- OAOS_COMPANY_VAULT_KEY_V<n>; OAOS_COMPANY_VAULT_KEY_CURRENT names the version
-- new envelopes use, and each row records its key_version. `vault.py rotate`
-- adds key n+1, makes it current, re-encrypts every active credential with it
-- in batches (one transaction per batch, compare-and-swap per row) and retires
-- the old key only after vault_key_usage() reads back zero rows on it.
--
-- The three functions are SECURITY DEFINER; db-definers.sql hands them to
-- oaos_company_maintenance (NOLOGIN, BYPASSRLS, no CREATE). Only the job login
-- oaos_company_maint may run them; the runtime role cannot. Plaintext and keys
-- never reach PostgreSQL: the procedure decrypts and seals in the Company
-- account and sends only the new envelope.
--
-- The C05 lifecycle trigger accepts a re-encryption (same generation, newer
-- key_version, fresh ciphertext and nonce) only from that role inside
-- vault_rekey_secret; the audit row records it as 'rekeyed' without envelope
-- material. A runtime session setting the marker itself still gets an ordinary
-- rotation (generation + 1).

CREATE OR REPLACE FUNCTION company.c05_secret_lifecycle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.owner_id IS NULL OR NEW.state <> 'active' OR NEW.generation <> 1 THEN
      RAISE EXCEPTION 'personal secret requires an active owner and first generation';
    END IF;
    -- Serialize storage with assistant revocation, including direct app SQL.
    PERFORM 1 FROM company.assistants a
      WHERE a.org_id = NEW.org_id AND a.owner_id = NEW.owner_id
        AND a.state = 'active' FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'active personal assistant required'; END IF;
    RETURN NEW;
  END IF;
  -- Vault key rotation (024): only the maintenance role, inside
  -- vault_rekey_secret, re-encrypts an active credential under a newer key
  -- version. The credential itself (generation, binding, lifecycle times) is
  -- unchanged, so assistant bindings (006) stay valid.
  IF current_setting('company.vault_rekey', true) = 'on'
     AND current_user = 'oaos_company_maintenance' THEN
    IF OLD.state <> 'active' OR NEW.state <> 'active'
       OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
       OR NEW.provider IS DISTINCT FROM OLD.provider
       OR NEW.purpose IS DISTINCT FROM OLD.purpose
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.generation IS DISTINCT FROM OLD.generation
       OR NEW.rotated_at IS DISTINCT FROM OLD.rotated_at
       OR NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
       OR NEW.key_version <= OLD.key_version
       OR NEW.ciphertext = OLD.ciphertext OR NEW.nonce = OLD.nonce THEN
      RAISE EXCEPTION 'vault re-encryption must only move an active secret to a newer key';
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.state <> 'active' OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
     OR NEW.provider IS DISTINCT FROM OLD.provider
     OR NEW.purpose IS DISTINCT FROM OLD.purpose
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'secret binding and revocation are immutable';
  END IF;
  IF NEW.state = 'revoked' THEN
    NEW.ciphertext := decode('00', 'hex');
    NEW.nonce := decode('00', 'hex');
    NEW.key_version := OLD.key_version;
    NEW.generation := OLD.generation + 1;
    NEW.rotated_at := OLD.rotated_at;
    NEW.revoked_at := now();
  ELSIF NEW.state = 'active' AND NEW.ciphertext IS DISTINCT FROM OLD.ciphertext
        AND NEW.nonce IS DISTINCT FROM OLD.nonce AND NEW.revoked_at IS NULL
        AND NEW.generation = OLD.generation THEN
    NEW.generation := OLD.generation + 1;
    NEW.rotated_at := now();
  ELSE
    RAISE EXCEPTION 'secret rotation requires fresh ciphertext and nonce';
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION company.c05_audit_secret_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE event_action text;
BEGIN
  IF TG_OP = 'UPDATE' AND current_setting('company.vault_rekey', true) = 'on'
     AND current_user = 'oaos_company_maintenance' THEN
    event_action := 'rekeyed';
  ELSIF TG_OP = 'INSERT' THEN event_action := 'stored';
  ELSIF NEW.state = 'revoked' THEN event_action := 'revoked';
  ELSE event_action := 'rotated'; END IF;
  INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type,
    action, resource_ref, decision, request_id, metadata)
  VALUES (NEW.org_id, NEW.owner_id, NEW.owner_id, 'personal_credential',
    event_action, NEW.id::text, 'allow', gen_random_uuid()::text,
    jsonb_build_object('generation', NEW.generation, 'key_version', NEW.key_version,
      'previous_key_version', CASE WHEN TG_OP = 'UPDATE' THEN OLD.key_version END));
  RETURN NEW;
END;
$$;

CREATE FUNCTION company.vault_key_usage()
RETURNS TABLE(key_version integer, active_rows bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = company, pg_catalog AS $$
  SELECT s.key_version, count(*) FROM company.secrets s
   WHERE s.state = 'active' GROUP BY s.key_version ORDER BY s.key_version;
$$;

CREATE FUNCTION company.vault_key_inventory(p_below_version integer, p_limit integer,
  p_after uuid DEFAULT NULL)
RETURNS TABLE(id uuid, org_id uuid, owner_id uuid, provider text, purpose text,
  ciphertext bytea, nonce bytea, key_version integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = company, pg_catalog AS $$
  SELECT s.id, s.org_id, s.owner_id, s.provider, s.purpose, s.ciphertext, s.nonce,
         s.key_version
    FROM company.secrets s
   WHERE s.state = 'active' AND s.key_version < p_below_version
     AND (p_after IS NULL OR s.id > p_after)
   ORDER BY s.id
   LIMIT LEAST(GREATEST(COALESCE(p_limit, 0), 0), 1000);
$$;

-- Compare-and-swap: the row must still be active, on p_old_version and hold
-- exactly p_old_ciphertext (a concurrent owner rotation wins; the procedure
-- picks the row up again in its next batch).
CREATE FUNCTION company.vault_rekey_secret(p_secret uuid, p_old_version integer,
  p_old_ciphertext bytea, p_ciphertext bytea, p_nonce bytea, p_version integer)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = company, pg_catalog AS $$
DECLARE
  target company.secrets%ROWTYPE;
BEGIN
  IF p_secret IS NULL OR p_old_version IS NULL OR p_version IS NULL
     OR p_version <= p_old_version OR p_old_ciphertext IS NULL
     OR p_ciphertext IS NULL OR length(p_ciphertext) <= 16
     OR p_nonce IS NULL OR length(p_nonce) <> 12 THEN
    RETURN 'invalid';
  END IF;
  SELECT s.* INTO target FROM company.secrets s WHERE s.id = p_secret FOR UPDATE;
  IF NOT FOUND OR target.state <> 'active' OR target.key_version <> p_old_version
     OR target.ciphertext <> p_old_ciphertext THEN
    RETURN 'changed';
  END IF;
  PERFORM set_config('company.org_id', target.org_id::text, true);
  PERFORM set_config('company.member_id', COALESCE(target.owner_id::text, ''), true);
  PERFORM set_config('company.vault_rekey', 'on', true);
  UPDATE company.secrets s
     SET ciphertext = p_ciphertext, nonce = p_nonce, key_version = p_version
   WHERE s.id = p_secret;
  PERFORM set_config('company.vault_rekey', 'off', true);
  RETURN 'rekeyed';
END;
$$;

REVOKE ALL ON FUNCTION company.vault_key_usage() FROM PUBLIC, oaos_company_runtime;
REVOKE ALL ON FUNCTION company.vault_key_inventory(integer, integer, uuid)
  FROM PUBLIC, oaos_company_runtime;
REVOKE ALL ON FUNCTION company.vault_rekey_secret(uuid, integer, bytea, bytea, bytea, integer)
  FROM PUBLIC, oaos_company_runtime;
GRANT EXECUTE ON FUNCTION company.vault_key_usage() TO oaos_company_maint;
GRANT EXECUTE ON FUNCTION company.vault_key_inventory(integer, integer, uuid) TO oaos_company_maint;
GRANT EXECUTE ON FUNCTION company.vault_rekey_secret(uuid, integer, bytea, bytea, bytea, integer)
  TO oaos_company_maint;

GRANT USAGE ON SCHEMA company TO oaos_company_maintenance, oaos_company_maint;
GRANT SELECT, UPDATE ON company.secrets TO oaos_company_maintenance;
GRANT INSERT ON company.audit_events TO oaos_company_maintenance;
