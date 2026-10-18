-- C08 adds permission-aware knowledge search on top of the C01 documents/chunks
-- schema. PostgreSQL full-text search is the p0 candidate generator; vectors are
-- intentionally out of scope.
--
-- The trusted server sets company.org_id/company.member_id from the
-- authenticated Project session. Search derives both values from the session,
-- checks that the owner is still active, resolves the source ACL before touching
-- chunks, and only then builds snippets. A document with an unknown, expired or
-- unavailable source ACL is never a candidate.

-- ACL freshness is recorded on the document. source_acl_version is the version
-- reported by the source; acl_generation invalidates previously synced ACL rows
-- immediately without guessing a source-specific version format.
ALTER TABLE company.documents
  ADD COLUMN acl_generation integer NOT NULL DEFAULT 1 CHECK (acl_generation > 0),
  ADD COLUMN acl_state text NOT NULL DEFAULT 'unknown'
    CHECK (acl_state IN ('known', 'unknown', 'unavailable')),
  ADD COLUMN acl_checked_at timestamptz,
  ADD COLUMN acl_expires_at timestamptz;

-- Personal documents have an intrinsic ACL: the owner recorded by C01. Existing
-- personal rows are already searchable under that rule. Shared rows stay unknown
-- until a source sync confirms their ACL, so the migration never widens access.
UPDATE company.documents
   SET acl_state = 'known', acl_checked_at = now()
 WHERE source = 'personal' AND owner_id IS NOT NULL;

-- Per-member source ACL. Rows only count while their acl_generation equals the
-- document's current generation, so invalidation is a single version bump.
CREATE TABLE company.document_acl (
  org_id uuid NOT NULL,
  document_id uuid NOT NULL,
  member_id uuid NOT NULL,
  acl_generation integer NOT NULL CHECK (acl_generation > 0),
  can_read boolean NOT NULL DEFAULT true,
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, document_id) REFERENCES company.documents(org_id, id)
    ON DELETE CASCADE,
  FOREIGN KEY (org_id, member_id) REFERENCES company.members(org_id, id),
  PRIMARY KEY (org_id, document_id, member_id)
);
CREATE INDEX document_acl_member_read
  ON company.document_acl(org_id, member_id, document_id)
  WHERE can_read;

CREATE FUNCTION company.c08_admin_ready() RETURNS boolean
LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT company.c03_admin_ready();
$$;

-- The single read decision used by search, chunk RLS and the C08 read-back.
-- Personal rows are readable only by their owner. Shared rows need a confirmed,
-- unexpired source ACL and a matching read entry for the current member.
CREATE FUNCTION company.c08_document_readable(p_document uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT company.c04_member_ready() AND EXISTS (
    SELECT 1 FROM company.documents d
     WHERE d.id = p_document
       AND d.org_id::text = current_setting('company.org_id', true)
       AND d.deleted_at IS NULL
       AND (
         (d.source = 'personal'
          AND d.owner_id::text = current_setting('company.member_id', true)
          AND EXISTS (
            SELECT 1 FROM company.assistants a
             WHERE a.org_id = d.org_id AND a.owner_id = d.owner_id
               AND a.state = 'active'))
         OR
         (d.source <> 'personal'
          AND d.acl_state = 'known'
          AND d.acl_expires_at IS NOT NULL
          AND d.acl_expires_at > now()
          AND EXISTS (
            SELECT 1 FROM company.document_acl a
             WHERE a.org_id = d.org_id AND a.document_id = d.id
               AND a.member_id::text = current_setting('company.member_id', true)
               AND a.acl_generation = d.acl_generation
               AND a.can_read))
       )
  );
$$;

ALTER TABLE company.document_acl ENABLE ROW LEVEL SECURITY;
ALTER TABLE company.document_acl FORCE ROW LEVEL SECURITY;
CREATE POLICY document_acl_member_read ON company.document_acl FOR SELECT USING (
  org_id::text = current_setting('company.org_id', true)
  AND member_id::text = current_setting('company.member_id', true)
  AND company.c04_member_ready()
);
CREATE POLICY document_acl_admin_all ON company.document_acl FOR ALL
  USING (company.c08_admin_ready())
  WITH CHECK (company.c08_admin_ready());

-- Chunks were previously readable by any member of the organization through
-- C01's coarse policy. Replace it so content follows the source ACL. Writes stay
-- available to an active administrator (sync worker) and to a personal owner;
-- an administrator who is not in the ACL can index but not read the content.
DROP POLICY chunk_scope ON company.chunks;
CREATE POLICY chunk_read ON company.chunks FOR SELECT USING (
  company.c08_document_readable(document_id)
);
-- The trusted sync worker runs in an active-administrator context and must be
-- able to replace derived chunks. Shared content is visible to administrators for
-- maintenance; personal content stays owner-only. Search still applies the source
-- ACL to every member, including administrators.
CREATE POLICY chunk_admin_read ON company.chunks FOR SELECT USING (
  company.c08_admin_ready() AND EXISTS (
    SELECT 1 FROM company.documents d
     WHERE d.id = document_id AND d.source <> 'personal')
);
CREATE POLICY chunk_insert ON company.chunks FOR INSERT WITH CHECK (
  company.c08_admin_ready() OR EXISTS (
    SELECT 1 FROM company.documents d
     WHERE d.id = document_id AND d.source = 'personal'
       AND d.owner_id::text = current_setting('company.member_id', true))
);
CREATE POLICY chunk_update ON company.chunks FOR UPDATE USING (
  company.c08_admin_ready() OR EXISTS (
    SELECT 1 FROM company.documents d
     WHERE d.id = document_id AND d.source = 'personal'
       AND d.owner_id::text = current_setting('company.member_id', true))
) WITH CHECK (
  company.c08_admin_ready() OR EXISTS (
    SELECT 1 FROM company.documents d
     WHERE d.id = document_id AND d.source = 'personal'
       AND d.owner_id::text = current_setting('company.member_id', true))
);
CREATE POLICY chunk_delete ON company.chunks FOR DELETE USING (
  company.c08_admin_ready() OR EXISTS (
    SELECT 1 FROM company.documents d
     WHERE d.id = document_id AND d.source = 'personal'
       AND d.owner_id::text = current_setting('company.member_id', true))
);

-- Direct writes must not bypass the ACL boundary. Shared documents and their
-- chunks are managed only by an active administrator; personal documents remain
-- owned by their member. This also prevents a member from reviving a shared row
-- or re-pointing a personal owner before C01's immutability trigger runs.
CREATE FUNCTION company.c08_document_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
  v_source text;
  v_owner uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    v_source := OLD.source;
    v_owner := OLD.owner_id;
  ELSE
    v_source := NEW.source;
    v_owner := NEW.owner_id;
  END IF;
  IF v_source = 'personal' THEN
    IF v_owner IS NULL
       OR v_owner::text <> current_setting('company.member_id', true) THEN
      RAISE EXCEPTION 'personal document changes require its owner';
    END IF;
  ELSIF NOT company.c08_admin_ready() THEN
    RAISE EXCEPTION 'shared document changes require an active administrator';
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;
CREATE TRIGGER documents_c08_guard BEFORE INSERT OR UPDATE OR DELETE
  ON company.documents FOR EACH ROW EXECUTE FUNCTION company.c08_document_guard();

-- Full-text index over the p0 candidate source. The config is explicit so the
-- expression is immutable and matches the query configuration used by C08.
CREATE INDEX chunks_body_fts ON company.chunks
  USING GIN (to_tsvector('simple', body));

CREATE FUNCTION company.c08_audit(p_org uuid, p_actor uuid, p_owner uuid,
  p_event_type text, p_action text, p_resource text, p_decision text, p_reason text,
  p_metadata jsonb)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
  INSERT INTO company.audit_events(org_id, actor_id, owner_id, event_type, action,
    resource_ref, decision, reason_code, request_id, metadata)
  VALUES (p_org, p_actor, p_owner, p_event_type, p_action, p_resource,
    p_decision, p_reason, gen_random_uuid()::text, COALESCE(p_metadata, '{}'::jsonb));
END;
$$;

-- Personal indexing derives the owner from the verified session. The source_id is
-- scoped to the owner so two members may use the same local identifier without a
-- UNIQUE collision. Re-indexing replaces the derived chunks atomically.
CREATE FUNCTION company.index_personal_document(p_source_id text, p_chunks text[])
RETURNS TABLE(decision text, document_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE
  org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
  member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
  scoped_source_id text;
  doc_id uuid;
  chunk_count integer;
BEGIN
  IF org IS NULL OR member IS NULL OR NOT company.c04_member_ready()
     OR p_source_id IS NULL OR btrim(p_source_id) = ''
     OR p_chunks IS NULL
     OR NOT EXISTS (
       SELECT 1 FROM company.assistants a
        WHERE a.org_id = org AND a.owner_id = member AND a.state = 'active') THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  scoped_source_id := member::text || ':' || p_source_id;
  INSERT INTO company.documents AS d(org_id, source, source_id, owner_id,
    source_acl_version, acl_state, acl_checked_at, acl_expires_at)
  VALUES (org, 'personal', scoped_source_id, member, 'owner', 'known', now(), NULL)
  ON CONFLICT (org_id, source, source_id) DO UPDATE
    SET updated_at = now(), deleted_at = NULL,
        acl_state = 'known', acl_checked_at = now(), acl_expires_at = NULL
    WHERE d.owner_id = member
  RETURNING d.id INTO doc_id;
  IF doc_id IS NULL THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  DELETE FROM company.chunks c WHERE c.document_id = doc_id;
  INSERT INTO company.chunks(org_id, document_id, ordinal, body)
    SELECT org, doc_id, (u.ord - 1)::integer, u.body
      FROM unnest(p_chunks) WITH ORDINALITY AS u(body, ord)
     WHERE u.body IS NOT NULL AND u.body <> '';
  GET DIAGNOSTICS chunk_count = ROW_COUNT;
  PERFORM company.c08_audit(org, member, member, 'knowledge_index', 'document_indexed',
    doc_id::text, 'allow', NULL,
    jsonb_build_object('source', 'personal', 'chunk_count', chunk_count));
  RETURN QUERY SELECT 'indexed'::text, doc_id;
END;
$$;

-- Shared indexing is the source-sync boundary. The trusted server runs it in an
-- active administrator context, supplies the confirmed ACL version, expiry and
-- explicit member list, and never derives read access from "shared with the org".
CREATE FUNCTION company.index_shared_document(p_source text, p_source_id text,
  p_source_acl_version text, p_acl_members uuid[], p_acl_expires_at timestamptz,
  p_chunks text[])
RETURNS TABLE(decision text, document_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE
  org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
  member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
  doc_id uuid;
  generation integer;
  member_count integer;
  chunk_count integer;
BEGIN
  IF org IS NULL OR member IS NULL OR NOT company.c08_admin_ready()
     OR p_source IS NULL OR p_source = '' OR p_source = 'personal'
     OR p_source_id IS NULL OR btrim(p_source_id) = ''
     OR p_source_acl_version IS NULL OR btrim(p_source_acl_version) = ''
     OR p_acl_expires_at IS NULL OR p_acl_expires_at <= now()
     OR p_chunks IS NULL THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  IF EXISTS (
    SELECT 1 FROM unnest(COALESCE(p_acl_members, '{}'::uuid[])) AS x(member_id)
     WHERE NOT EXISTS (
       SELECT 1 FROM company.members m
        WHERE m.org_id = org AND m.id = x.member_id AND m.state = 'active')
  ) THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  INSERT INTO company.documents AS d(org_id, source, source_id, owner_id,
    source_acl_version, acl_state, acl_checked_at, acl_expires_at, acl_generation)
  VALUES (org, p_source, p_source_id, NULL, p_source_acl_version, 'known', now(),
    p_acl_expires_at, 1)
  ON CONFLICT (org_id, source, source_id) DO UPDATE
    SET source_acl_version = EXCLUDED.source_acl_version,
        acl_state = 'known', acl_checked_at = now(),
        acl_expires_at = EXCLUDED.acl_expires_at,
        acl_generation = d.acl_generation + 1,
        deleted_at = NULL, updated_at = now()
    WHERE d.owner_id IS NULL
  RETURNING d.id INTO doc_id;
  IF doc_id IS NULL THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid; RETURN;
  END IF;
  SELECT d.acl_generation INTO generation FROM company.documents d WHERE d.id = doc_id;
  DELETE FROM company.document_acl a WHERE a.document_id = doc_id;
  INSERT INTO company.document_acl(org_id, document_id, member_id,
    acl_generation, can_read)
    SELECT org, doc_id, x.member_id, generation, true
      FROM (SELECT DISTINCT member_id FROM unnest(COALESCE(p_acl_members, '{}'::uuid[]))
              AS u(member_id)) x;
  GET DIAGNOSTICS member_count = ROW_COUNT;
  DELETE FROM company.chunks c WHERE c.document_id = doc_id;
  INSERT INTO company.chunks(org_id, document_id, ordinal, body)
    SELECT org, doc_id, (u.ord - 1)::integer, u.body
      FROM unnest(p_chunks) WITH ORDINALITY AS u(body, ord)
     WHERE u.body IS NOT NULL AND u.body <> '';
  GET DIAGNOSTICS chunk_count = ROW_COUNT;
  PERFORM company.c08_audit(org, member, NULL, 'knowledge_index', 'document_indexed',
    doc_id::text, 'allow', NULL,
    jsonb_build_object('source', p_source, 'acl_version', p_source_acl_version,
      'acl_member_count', member_count, 'chunk_count', chunk_count));
  RETURN QUERY SELECT 'indexed'::text, doc_id;
END;
$$;

-- A source change, unshare or transient source failure bumps the generation and
-- marks the ACL unknown. Candidates disappear on the next request; the document
-- returns only after a successful ACL re-confirmation.
CREATE FUNCTION company.invalidate_document_acl(p_document uuid, p_reason text DEFAULT 'source_change')
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE
  org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
  member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
BEGIN
  IF org IS NULL OR member IS NULL OR NOT company.c08_admin_ready()
     OR p_reason IS NULL OR p_reason = '' THEN
    RETURN 'denied';
  END IF;
  UPDATE company.documents d
     SET acl_generation = d.acl_generation + 1, acl_state = 'unknown',
         acl_checked_at = NULL, acl_expires_at = NULL, updated_at = now()
   WHERE d.id = p_document AND d.source <> 'personal' AND d.deleted_at IS NULL;
  IF NOT FOUND THEN RETURN 'denied'; END IF;
  DELETE FROM company.document_acl a WHERE a.document_id = p_document;
  PERFORM company.c08_audit(org, member, NULL, 'knowledge_index', 'acl_invalidated',
    p_document::text, 'applied', p_reason, '{}'::jsonb);
  RETURN 'invalidated';
END;
$$;

-- Deletion blocks candidates first (deleted_at), then removes the derived chunks.
-- Keeping the document row preserves the audit reference without retaining body
-- text. Only an administrator may delete shared rows; a personal owner may delete
-- their own row.
CREATE FUNCTION company.delete_document(p_document uuid) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE
  org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
  member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
  doc_source text;
  doc_owner uuid;
BEGIN
  IF org IS NULL OR member IS NULL OR NOT company.c04_member_ready() THEN
    RETURN 'denied';
  END IF;
  SELECT d.source, d.owner_id INTO doc_source, doc_owner
    FROM company.documents d
   WHERE d.id = p_document AND d.org_id = org AND d.deleted_at IS NULL
   FOR UPDATE;
  IF NOT FOUND THEN RETURN 'denied'; END IF;
  IF doc_source = 'personal' THEN
    IF doc_owner IS DISTINCT FROM member THEN RETURN 'denied'; END IF;
  ELSIF NOT company.c08_admin_ready() THEN
    RETURN 'denied';
  END IF;
  UPDATE company.documents d
     SET deleted_at = now(), acl_state = 'unavailable',
         acl_generation = d.acl_generation + 1, updated_at = now()
   WHERE d.id = p_document;
  DELETE FROM company.chunks c WHERE c.document_id = p_document;
  DELETE FROM company.document_acl a WHERE a.document_id = p_document;
  PERFORM company.c08_audit(org, member,
    CASE WHEN doc_source = 'personal' THEN member ELSE NULL END,
    'knowledge_index', 'document_deleted', p_document::text, 'applied', NULL,
    jsonb_build_object('source', doc_source));
  RETURN 'deleted';
END;
$$;

-- Search always derives org and member from the session. Candidates are built
-- from chunks only after c08_document_readable passes; the candidate set is
-- re-checked before snippets are returned. Audit metadata never contains the
-- query, document body or snippet.
CREATE FUNCTION company.search_documents(p_query text, p_limit integer DEFAULT 10)
RETURNS TABLE(decision text, document_id uuid, chunk_id uuid, ordinal integer,
  snippet text)
LANGUAGE plpgsql VOLATILE SECURITY INVOKER AS $$
DECLARE
  org uuid := NULLIF(current_setting('company.org_id', true), '')::uuid;
  member uuid := NULLIF(current_setting('company.member_id', true), '')::uuid;
  candidate_ids uuid[];
  invalid_count integer;
  match_count integer;
BEGIN
  IF org IS NULL OR member IS NULL THEN
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::uuid, NULL::integer,
      NULL::text; RETURN;
  END IF;
  IF NOT company.c04_member_ready() THEN
    IF EXISTS (SELECT 1 FROM company.members m
                WHERE m.org_id = org AND m.id = member) THEN
      PERFORM company.c08_audit(org, member, member, 'search_access',
        'search_denied', NULL, 'deny', 'owner_inactive',
        jsonb_build_object('query_length', COALESCE(length(p_query), 0),
          'limit', COALESCE(p_limit, 0)));
    END IF;
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::uuid, NULL::integer,
      NULL::text; RETURN;
  END IF;
  IF p_query IS NULL OR btrim(p_query) = '' OR p_limit IS NULL
     OR p_limit < 1 OR p_limit > 50 THEN
    PERFORM company.c08_audit(org, member, member, 'search_access', 'search_denied', NULL,
      'deny', 'invalid_request',
      jsonb_build_object('query_length', COALESCE(length(p_query), 0),
        'limit', COALESCE(p_limit, 0)));
    RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::uuid, NULL::integer,
      NULL::text; RETURN;
  END IF;

  SELECT COALESCE(array_agg(DISTINCT c.document_id), '{}'::uuid[])
    INTO candidate_ids
    FROM company.chunks c
   WHERE company.c08_document_readable(c.document_id)
     AND to_tsvector('simple', c.body) @@ plainto_tsquery('simple', p_query);

  IF cardinality(candidate_ids) > 0 THEN
    SELECT count(*) INTO invalid_count
      FROM unnest(candidate_ids) AS x(document_id)
     WHERE NOT company.c08_document_readable(x.document_id);
    IF invalid_count > 0 THEN
      PERFORM company.c08_audit(org, member, member, 'search_access', 'search_denied', NULL,
        'deny', 'acl_changed',
        jsonb_build_object('query_length', length(p_query), 'limit', p_limit));
      RETURN QUERY SELECT 'denied'::text, NULL::uuid, NULL::uuid, NULL::integer,
        NULL::text; RETURN;
    END IF;
  END IF;

  SELECT count(*) INTO match_count
    FROM company.chunks c
   WHERE company.c08_document_readable(c.document_id)
     AND to_tsvector('simple', c.body) @@ plainto_tsquery('simple', p_query);

  PERFORM company.c08_audit(org, member, member, 'search_access', 'search', NULL, 'allow', NULL,
    jsonb_build_object('query_length', length(p_query), 'limit', p_limit,
      'result_count', LEAST(match_count, p_limit)));

  RETURN QUERY
    SELECT 'allowed'::text, c.document_id, c.id, c.ordinal,
           ts_headline('simple', c.body, q.query,
             'StartSel=, StopSel=, MaxWords=24, MinWords=4')
      FROM company.chunks c
      CROSS JOIN (SELECT plainto_tsquery('simple', p_query) AS query) q
     WHERE company.c08_document_readable(c.document_id)
       AND to_tsvector('simple', c.body) @@ q.query
     ORDER BY ts_rank(to_tsvector('simple', c.body), q.query) DESC,
              c.document_id, c.ordinal
     LIMIT p_limit;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'allowed'::text, NULL::uuid, NULL::uuid, NULL::integer,
      NULL::text;
  END IF;
END;
$$;
