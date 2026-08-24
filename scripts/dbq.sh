#!/usr/bin/env bash
# dbq.sh — Run a SQL query against dev or prod RamboQuant DB via SSH.
# Usage: ./scripts/dbq.sh <dev|prod> "SQL"
# Credentials are read from the server's secrets.yaml at SSH time — never stored locally.

set -euo pipefail

ENV="${1:-}"
SQL="${2:-}"

if [[ -z "$ENV" || -z "$SQL" ]]; then
  echo "Usage: $0 <dev|prod> \"SQL\"" >&2
  exit 1
fi

case "$ENV" in
  dev)  DB="ramboq_dev"  ;;
  prod) DB="ramboq"      ;;
  *)    echo "Unknown env '$ENV'. Use 'dev' or 'prod'." >&2; exit 1 ;;
esac

# Pass SQL via stdin to avoid quoting issues with special characters.
# All credential handling happens on the server — nothing is stored locally.
echo "$SQL" | ssh ramboq "
  set -euo pipefail
  SECRETS=/opt/ramboq/backend/config/secrets.yaml
  DB_USER=\$(python3 -c \"import yaml; d=yaml.safe_load(open('\$SECRETS')); print(d['db_user'])\")
  DB_PASS=\$(python3 -c \"import yaml; d=yaml.safe_load(open('\$SECRETS')); print(d['db_password'])\")
  PGPASSWORD=\"\$DB_PASS\" psql -h 127.0.0.1 -U \"\$DB_USER\" -d \"$DB\"
"
