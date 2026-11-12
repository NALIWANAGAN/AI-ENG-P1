#!/bin/sh
set -e
psql -v ON_ERROR_STOP=1 \
     -v encoder_pw="$POSTGRES_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -f /sql/02_database_role.sql