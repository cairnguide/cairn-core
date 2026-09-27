#!/usr/bin/env bash
# Sets up a development environment in one step. Safe to run again.
#   1. a Python virtual environment in .venv with the API, test, lint, and loader dependencies
#   2. a "cairn" database with every migration (plus the two optional ones the API needs)
#   3. the task and journey templates (drafts allowed, development only)
#   4. the cairn_api_login role with a fresh random password
#   5. .env, copied from .env.example on the first run, with DATABASE_URL pointing at that role
#
# Usage: scripts/dev-setup.sh
# ADMIN_URL defaults to the devcontainer's Postgres (postgres:postgres@localhost:5432). It needs
# permission to create databases and roles. Never point it at real data.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
ADMIN_URL="${ADMIN_URL:-postgresql://postgres:postgres@localhost:5432/postgres}"
DB_NAME="${CAIRN_DB_NAME:-cairn}"

echo "== Python environment (.venv)"
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e 'api[test,lint]' -r database/tools/requirements.txt

command -v psql >/dev/null || { echo "psql is missing. Install the PostgreSQL client (see README.md)." >&2; exit 1; }

echo "== Waiting for Postgres"
for _ in $(seq 1 30); do
  psql "$ADMIN_URL" -XtAq -c "SELECT 1" >/dev/null 2>&1 && break
  sleep 1
done
psql "$ADMIN_URL" -XtAq -c "SELECT 1" >/dev/null

echo "== Database $DB_NAME"
if [[ "$(psql "$ADMIN_URL" -XtA -c "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'")" != "1" ]]; then
  psql "$ADMIN_URL" -Xq -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$DB_NAME\""
fi
OWNER_URL="$(.venv/bin/python - "$ADMIN_URL" "$DB_NAME" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit
u = urlsplit(sys.argv[1])
print(urlunsplit((u.scheme, u.netloc, "/" + sys.argv[2], u.query, u.fragment)))
PY
)"

echo "== Migrations"
DATABASE_URL="$OWNER_URL" database/db/apply.sh context_items_jsonb context_items_read_only

echo "== Templates"
DATABASE_URL="$OWNER_URL" .venv/bin/python database/tools/load_templates.py --allow-unreviewed --git-release dev

echo "== API login role"
APP_URL="$(DATABASE_URL="$OWNER_URL" .venv/bin/python database/tools/create_login_role.py --generate | tail -n 1)"

echo "== .env"
[[ -f .env ]] || cp .env.example .env
# Both URLs use only URL-safe characters, so "|" is a safe sed delimiter.
sed -i.bak -e "s|^DATABASE_URL=.*|DATABASE_URL=$APP_URL|" \
           -e "s|^CAIRN_OWNER_DATABASE_URL=.*|CAIRN_OWNER_DATABASE_URL=$OWNER_URL|" .env
rm -f .env.bak

if command -v npm >/dev/null && [[ -f cloudflare/package-lock.json ]]; then
  echo "== Cloudflare Worker dependencies"
  (cd cloudflare && npm ci --no-audit --no-fund --loglevel=error)
fi

echo
echo "Ready. Start the API with:  make run   (then open http://localhost:8000/docs)"
