-- oaos-applied-with: 8
-- G13 usage billing goes through the C07 JIT approval (audit P1-6). Applied by
-- C07 and every later phase; C16 checks it through version 8.
--
-- Before: C16 accepted an environment flag as "approval" and recorded the
-- administrator the installer happened to pick as the approver. Now a
-- usage-billed model row is only written in the transaction that consumed an
-- llm.billing.usage approval for exactly this provider, model and cap; the
-- stored approver is the administrator who decided that approval and the
-- approval id is kept on the model.
ALTER TABLE company.llm_models ADD COLUMN billing_approval_id uuid
  REFERENCES company.approvals(id);

CREATE FUNCTION company.g13_billing_digest(p_provider text, p_model text, p_cap numeric)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT encode(sha256(convert_to(
    COALESCE(p_provider, '') || '|' || COALESCE(p_model, '') || '|'
      || COALESCE(p_cap::numeric(12,2)::text, ''), 'UTF8')), 'hex');
$$;

-- Fires before llm_models_c16_validate (trigger names sort alphabetically).
CREATE FUNCTION company.c16_billing_approval() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE appr company.approvals%ROWTYPE;
BEGIN
  IF NEW.billing_mode <> 'usage' THEN
    NEW.billing_approval_id := NULL;
    RETURN NEW;
  END IF;
  -- Revocation, or a rerun that keeps the approved cap, keeps the approval.
  IF TG_OP = 'UPDATE' AND OLD.billing_approval_id IS NOT NULL
     AND (NEW.state = 'revoked' OR NEW.monthly_cap_usd = OLD.monthly_cap_usd) THEN
    NEW.billing_approval_id := OLD.billing_approval_id;
    NEW.billing_approved_by := OLD.billing_approved_by;
    NEW.billing_approved_at := OLD.billing_approved_at;
    RETURN NEW;
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.state = 'revoked' THEN
    RETURN NEW;
  END IF;
  SELECT a.* INTO appr FROM company.approvals a
   WHERE a.org_id = NEW.org_id AND a.action = 'llm.billing.usage'
     AND a.state = 'consumed' AND a.consumed_at = now()
     AND a.requester_id::text = current_setting('company.member_id', true)
     AND a.resource_digest = company.g13_billing_digest(NEW.provider, NEW.model_name,
           NEW.monthly_cap_usd)
   ORDER BY a.consumed_at DESC, a.id LIMIT 1;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'usage billing requires a consumed llm.billing.usage approval';
  END IF;
  NEW.billing_approval_id := appr.id;
  NEW.billing_approved_by := appr.approver_id;
  NEW.billing_approved_at := now();
  RETURN NEW;
END;
$$;
CREATE TRIGGER llm_models_c16_billing_approval BEFORE INSERT OR UPDATE ON company.llm_models
  FOR EACH ROW EXECUTE FUNCTION company.c16_billing_approval();
