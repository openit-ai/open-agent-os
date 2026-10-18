-- C08 uses synthetic fixtures and always rolls back. Bodies and snippets are never
-- printed; failed checks raise only a reason code.
BEGIN;
DO $verify$
DECLARE
  org_a uuid := gen_random_uuid();
  org_b uuid := gen_random_uuid();
  admin_a uuid := gen_random_uuid();
  alice uuid := gen_random_uuid();
  bob uuid := gen_random_uuid();
  carol uuid := gen_random_uuid();
  doc_shared uuid;
  doc_bob uuid;
  doc_personal uuid;
  decision text;
  source text;
  snippet text;
  row_count integer;
  match_count integer;
BEGIN
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_a, 'c08-' || org_a::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
    (admin_a, org_a, 'c08-admin-' || admin_a::text, 'admin', 'active'),
    (alice, org_a, 'c08-alice-' || alice::text, 'member', 'active'),
    (bob, org_a, 'c08-bob-' || bob::text, 'member', 'active');

  -- A shared document is indexed with a confirmed source ACL for alice only.
  SELECT r.decision, r.document_id INTO decision, doc_shared
    FROM company.index_shared_document('project', 'doc-shared', 'acl-1',
      ARRAY[alice], now() + interval '5 minutes',
      ARRAY['report sharedpolicy']) r;
  IF decision IS DISTINCT FROM 'indexed' OR doc_shared IS NULL THEN
    RAISE EXCEPTION 'shared document indexing failed';
  END IF;

  SELECT r.decision, r.document_id INTO decision, doc_bob
    FROM company.index_shared_document('project', 'doc-bob', 'acl-1',
      ARRAY[bob], now() + interval '5 minutes',
      ARRAY['report bobpolicy']) r;
  IF decision IS DISTINCT FROM 'indexed' OR doc_bob IS NULL THEN
    RAISE EXCEPTION 'second shared document indexing failed';
  END IF;

  PERFORM set_config('company.member_id', alice::text, true);
  SELECT r.decision, r.document_id INTO decision, doc_personal
    FROM company.index_personal_document('note-1', ARRAY['report personalpolicy']) r;
  IF decision IS DISTINCT FROM 'indexed' OR doc_personal IS NULL THEN
    RAISE EXCEPTION 'personal document indexing failed';
  END IF;

  -- Owner sees the granted shared document and the personal document only.
  SELECT count(*) FILTER (WHERE s.document_id IS NOT NULL), count(*) INTO
    row_count, match_count FROM company.search_documents('report', 10) s;
  IF row_count IS DISTINCT FROM 2 OR match_count IS DISTINCT FROM 2 THEN
    RAISE EXCEPTION 'owner search returned an unexpected candidate count';
  END IF;
  IF EXISTS (SELECT 1 FROM company.search_documents('report', 10) s
              WHERE s.document_id = doc_bob) THEN
    RAISE EXCEPTION 'owner search crossed the source ACL';
  END IF;
  SELECT s.snippet INTO snippet FROM company.search_documents('personalpolicy', 10) s
   WHERE s.document_id = doc_personal;
  IF snippet IS NULL OR snippet NOT LIKE '%personalpolicy%' THEN
    RAISE EXCEPTION 'owner personal snippet was not built';
  END IF;

  -- A same-organization member without a grant sees no shared content.
  PERFORM set_config('company.member_id', bob::text, true);
  SELECT count(*) FILTER (WHERE s.document_id IS NOT NULL), count(*) INTO
    row_count, match_count FROM company.search_documents('report', 10) s;
  IF row_count IS DISTINCT FROM 1 OR match_count IS DISTINCT FROM 1
     OR EXISTS (SELECT 1 FROM company.search_documents('report', 10) s
                 WHERE s.document_id IN (doc_shared, doc_personal)) THEN
    RAISE EXCEPTION 'ungranted member search crossed the source ACL';
  END IF;
  IF (SELECT count(*) FROM company.chunks c WHERE c.document_id = doc_shared) <> 0 THEN
    RAISE EXCEPTION 'ungranted member read shared chunks directly';
  END IF;
  SELECT count(*) FILTER (WHERE s.document_id IS NOT NULL), count(*) INTO
    row_count, match_count FROM company.search_documents('personalpolicy', 10) s;
  IF row_count IS DISTINCT FROM 0 OR match_count IS DISTINCT FROM 1 THEN
    RAISE EXCEPTION 'personal content was exposed to another member';
  END IF;

  -- Shared indexing and ACL invalidation are administrator-only.
  SELECT r.decision INTO decision FROM company.index_shared_document('project',
    'doc-forged', 'acl-1', ARRAY[bob], now() + interval '5 minutes',
    ARRAY['forged']) r;
  IF decision IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'member indexed a shared document';
  END IF;
  IF company.invalidate_document_acl(doc_shared, 'source_change') IS DISTINCT FROM 'denied'
     OR company.delete_document(doc_shared) IS DISTINCT FROM 'denied'
     OR company.delete_document(doc_personal) IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'member changed shared or foreign personal ACL state';
  END IF;

  -- Revoking the source ACL takes effect on the next request.
  PERFORM set_config('company.member_id', admin_a::text, true);
  SELECT r.decision INTO decision FROM company.index_shared_document('project',
    'doc-shared', 'acl-2', ARRAY[bob], now() + interval '5 minutes',
    ARRAY['report sharedpolicy']) r;
  IF decision IS DISTINCT FROM 'indexed' THEN
    RAISE EXCEPTION 'ACL change sync failed';
  END IF;
  PERFORM set_config('company.member_id', alice::text, true);
  SELECT count(*) FILTER (WHERE s.document_id IS NOT NULL) INTO row_count
    FROM company.search_documents('sharedpolicy', 10) s;
  IF row_count IS DISTINCT FROM 0 THEN
    RAISE EXCEPTION 'revoked shared ACL stayed searchable';
  END IF;
  PERFORM set_config('company.member_id', bob::text, true);
  SELECT count(*) FILTER (WHERE s.document_id IS NOT NULL) INTO row_count
    FROM company.search_documents('sharedpolicy', 10) s;
  IF row_count IS DISTINCT FROM 1 THEN
    RAISE EXCEPTION 'newly granted shared ACL was not searchable';
  END IF;
  IF (SELECT count(*) FROM company.chunks c WHERE c.document_id = doc_shared) = 0 THEN
    RAISE EXCEPTION 'granted member could not read shared chunks';
  END IF;

  -- An unconfirmed source ACL (source change/outage) removes candidates.
  PERFORM set_config('company.member_id', admin_a::text, true);
  IF company.invalidate_document_acl(doc_bob, 'source_unavailable')
     IS DISTINCT FROM 'invalidated' THEN
    RAISE EXCEPTION 'ACL invalidation failed';
  END IF;
  PERFORM set_config('company.member_id', bob::text, true);
  SELECT count(*) FILTER (WHERE s.document_id IS NOT NULL) INTO row_count
    FROM company.search_documents('bobpolicy', 10) s;
  IF row_count IS DISTINCT FROM 0 THEN
    RAISE EXCEPTION 'invalidated ACL stayed searchable';
  END IF;

  -- An expired ACL cache is denied without producing a snippet.
  PERFORM set_config('company.member_id', admin_a::text, true);
  SELECT r.decision INTO decision FROM company.index_shared_document('project',
    'doc-bob', 'acl-3', ARRAY[bob], now() + interval '5 minutes',
    ARRAY['report bobpolicy']) r;
  IF decision IS DISTINCT FROM 'indexed' THEN
    RAISE EXCEPTION 'ACL re-confirmation failed';
  END IF;
  UPDATE company.documents SET acl_expires_at = now() - interval '1 second'
   WHERE id = doc_bob;
  PERFORM set_config('company.member_id', bob::text, true);
  SELECT count(*) FILTER (WHERE s.document_id IS NOT NULL) INTO row_count
    FROM company.search_documents('bobpolicy', 10) s;
  IF row_count IS DISTINCT FROM 0 THEN
    RAISE EXCEPTION 'expired ACL cache stayed searchable';
  END IF;

  -- Deletion blocks candidates first and removes derived chunks.
  PERFORM set_config('company.member_id', admin_a::text, true);
  SELECT r.decision INTO decision FROM company.index_shared_document('project',
    'doc-shared', 'acl-4', ARRAY[bob], now() + interval '5 minutes',
    ARRAY['report sharedpolicy']) r;
  IF company.delete_document(doc_shared) IS DISTINCT FROM 'deleted' THEN
    RAISE EXCEPTION 'shared document deletion failed';
  END IF;
  IF (SELECT count(*) FROM company.chunks c WHERE c.document_id = doc_shared) <> 0
     OR (SELECT count(*) FROM company.documents d
          WHERE d.id = doc_shared AND d.deleted_at IS NOT NULL) <> 1 THEN
    RAISE EXCEPTION 'deleted document retained chunks or lacked deleted_at';
  END IF;
  PERFORM set_config('company.member_id', bob::text, true);
  SELECT count(*) FILTER (WHERE s.document_id IS NOT NULL) INTO row_count
    FROM company.search_documents('sharedpolicy', 10) s;
  IF row_count IS DISTINCT FROM 0 THEN
    RAISE EXCEPTION 'deleted document stayed searchable';
  END IF;

  -- Cross-tenant access and member-list validation are denied.
  PERFORM set_config('company.org_id', org_b::text, true);
  PERFORM set_config('company.member_id', carol::text, true);
  INSERT INTO company.organizations(id, slug) VALUES (org_b, 'c08-' || org_b::text);
  INSERT INTO company.members(id, org_id, project_user_id, role, state)
    VALUES (carol, org_b, 'c08-carol-' || carol::text, 'admin', 'active');
  SELECT count(*) FILTER (WHERE s.document_id IS NOT NULL) INTO row_count
    FROM company.search_documents('report', 10) s;
  IF row_count IS DISTINCT FROM 0
     OR company.c08_document_readable(doc_shared)
     OR (SELECT r.decision FROM company.index_shared_document('project',
           'foreign-member', 'acl-1', ARRAY[alice], now() + interval '5 minutes',
           ARRAY['foreign']) r) IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'cross-tenant search or ACL validation failed';
  END IF;

  -- Audit records access decisions and index changes without content.
  PERFORM set_config('company.org_id', org_a::text, true);
  PERFORM set_config('company.member_id', admin_a::text, true);
  IF NOT EXISTS (SELECT 1 FROM company.audit_events e
                  WHERE e.event_type = 'search_access' AND e.action = 'search'
                    AND e.decision = 'allow')
     OR NOT EXISTS (SELECT 1 FROM company.audit_events e
                  WHERE e.event_type = 'knowledge_index' AND e.action = 'document_indexed')
     OR NOT EXISTS (SELECT 1 FROM company.audit_events e
                  WHERE e.event_type = 'knowledge_index' AND e.action = 'acl_invalidated')
     OR NOT EXISTS (SELECT 1 FROM company.audit_events e
                  WHERE e.event_type = 'knowledge_index' AND e.action = 'document_deleted') THEN
    RAISE EXCEPTION 'search or index audit event missing';
  END IF;
  IF EXISTS (SELECT 1 FROM company.audit_events e
              WHERE e.metadata ? 'body' OR e.metadata ? 'snippet'
                 OR e.metadata ? 'query'
                 OR e.metadata::text LIKE '%sharedpolicy%'
                 OR e.metadata::text LIKE '%bobpolicy%'
                 OR e.metadata::text LIKE '%personalpolicy%') THEN
    RAISE EXCEPTION 'audit metadata leaked search content';
  END IF;

  -- Suspending an owner blocks search immediately and records no content.
  UPDATE company.members SET state = 'suspended' WHERE id = alice;
  PERFORM set_config('company.member_id', alice::text, true);
  SELECT s.decision INTO decision FROM company.search_documents('report', 10) s;
  IF decision IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'suspended owner search was not denied';
  END IF;
  SELECT s.decision INTO source FROM company.search_documents('', 10) s;
  IF source IS DISTINCT FROM 'denied' THEN
    RAISE EXCEPTION 'invalid search request was not denied';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM company.audit_events e
                  WHERE e.event_type = 'search_access' AND e.action = 'search_denied'
                    AND e.reason_code = 'owner_inactive' AND e.actor_id = alice) THEN
    RAISE EXCEPTION 'inactive owner denial was not audited';
  END IF;
END;
$verify$;
ROLLBACK;
