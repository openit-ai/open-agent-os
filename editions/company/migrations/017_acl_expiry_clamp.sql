-- oaos-applied-with: 9
-- C08 ACL freshness bound (audit P2-15). Applied by C08 and every later phase.
-- index_shared_document() accepted any future p_acl_expires_at, so a sync could
-- keep a stale source ACL searchable for days. The design re-checks the source
-- at least every 5 minutes; a shared document's confirmed ACL now expires no
-- later than 5 minutes after it was written, through the function or direct SQL.
-- Personal documents keep their intrinsic owner ACL (no expiry).
CREATE FUNCTION company.c08_acl_expiry_clamp() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.source <> 'personal' AND NEW.acl_expires_at IS NOT NULL
     AND NEW.acl_expires_at > now() + interval '5 minutes' THEN
    NEW.acl_expires_at := now() + interval '5 minutes';
  END IF;
  RETURN NEW;
END;
$$;
-- Named to fire before documents_c08_guard (alphabetical trigger order).
CREATE TRIGGER documents_c08_acl_clamp BEFORE INSERT OR UPDATE OF acl_expires_at, source
  ON company.documents FOR EACH ROW EXECUTE FUNCTION company.c08_acl_expiry_clamp();
