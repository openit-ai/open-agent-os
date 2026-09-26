#!/usr/bin/env bash
set -Eeuo pipefail
psql -v ON_ERROR_STOP=1 --username postgres --set=mm_password="$MM_DB_PASSWORD" --set=outline_password="$OUTLINE_DB_PASSWORD" <<'SQL'
CREATE USER mattermost WITH PASSWORD :'mm_password';
CREATE DATABASE mattermost OWNER mattermost;
CREATE USER outline WITH PASSWORD :'outline_password';
CREATE DATABASE outline OWNER outline;
SQL
