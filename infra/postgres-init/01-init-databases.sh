#!/bin/bash
# Runs once, on the first start of the db container (empty data volume only).
# Creates one database and one dedicated user per service so no service can
# touch another service's tables.
set -euo pipefail

create_service_db() {
    local name="$1"
    local password="$2"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
        CREATE USER sakura_${name} WITH PASSWORD '${password}';
        CREATE DATABASE sakura_${name} OWNER sakura_${name};
        REVOKE ALL ON DATABASE sakura_${name} FROM PUBLIC;
EOSQL
}

create_service_db settings "${SETTINGS_DB_PASSWORD}"
create_service_db ledger   "${LEDGER_DB_PASSWORD}"
create_service_db budget   "${BUDGET_DB_PASSWORD}"
create_service_db stocks   "${STOCKS_DB_PASSWORD}"
create_service_db receipts "${RECEIPTS_DB_PASSWORD}"
