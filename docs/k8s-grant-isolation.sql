-- Open Agent OS — DB GRANT isolation (v1.6 §27 / SECURITY.md)
-- Purpose: ensure the openagentos application role has NO privileges on
-- external databases (mattermost, outline) that may reside on the same
-- Postgres cluster. Run as superuser / rds_superuser after provisioning.
--
-- Generated for: deploy/k8s/postgres-statefulset.yaml, deploy/docker-compose.*.yml,
-- and any customer-owned cluster that shares a Postgres instance with
-- Mattermost / Outline. Safe to re-run (idempotent REVOKE).

-- Revoke all on the external DBs from openagentos (if they exist)
-- Note: REVOKE ALL ON DATABASE only revokes CONNECT/CREATE/TEMP; schema-level
-- privileges must also be revoked if previously granted.

-- Mattermost DB isolation
REVOKE ALL ON DATABASE mattermost FROM openagentos;
REVOKE CONNECT ON DATABASE mattermost FROM openagentos;

-- Outline DB isolation
REVOKE ALL ON DATABASE outline FROM openagentos;
REVOKE CONNECT ON DATABASE outline FROM openagentos;

-- If cross-DB schemas were ever granted, also revoke (no-op if never granted):
-- Run these connected to each external DB:
--   REVOKE ALL ON SCHEMA public FROM openagentos;
--   REVOKE ALL ON ALL TABLES IN SCHEMA public FROM openagentos;
--   REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM openagentos;

-- Verification (as superuser):
--   SELECT has_database_privilege('openagentos', 'mattermost', 'connect'); -- expect f
--   SELECT has_database_privilege('openagentos', 'outline', 'connect');    -- expect f

-- K8s ConfigMap reference: this file is mounted/documented via
-- deploy/k8s/configmap.yaml comment and deploy/scripts/verify-16a.sh.
-- See also: deploy/k8s/README.md § Grant isolation and SECURITY.md § DB isolation.
