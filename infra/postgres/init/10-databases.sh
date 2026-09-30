#!/usr/bin/env bash
# Runs once, on an empty data directory, as the postgres superuser (docker-entrypoint-initdb.d).
# Creates the three service roles and databases, then Lakekeeper's extensions.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v lakekeeper_pw="$LAKEKEEPER_DB_PASSWORD" \
  -v airflow_pw="$AIRFLOW_DB_PASSWORD" \
  -v shopstream_pw="$SHOPSTREAM_DB_PASSWORD" <<'SQL'
CREATE ROLE lakekeeper LOGIN PASSWORD :'lakekeeper_pw';
CREATE ROLE airflow LOGIN PASSWORD :'airflow_pw';
CREATE ROLE shopstream LOGIN PASSWORD :'shopstream_pw';

CREATE DATABASE lakekeeper OWNER lakekeeper;
CREATE DATABASE airflow OWNER airflow;
CREATE DATABASE shopstream OWNER shopstream;

\connect lakekeeper
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE EXTENSION IF NOT EXISTS btree_gist;
SQL
