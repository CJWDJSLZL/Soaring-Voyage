#!/bin/sh
set -eu

# This runs only during PostgreSQL cluster initialization. The application role
# is deliberately fixed and non-superuser; only its password comes from .env.
psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=app_password="$APP_DB_PASSWORD" <<'SQL'
CREATE ROLE soaring_voyage_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD :'app_password';
GRANT CONNECT ON DATABASE :"DBNAME" TO soaring_voyage_app;
SQL
