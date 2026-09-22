#!/usr/bin/env bash
# Builds a scratch database, applies migrations, runs the loader and the checks, then drops it.
# Usage: ADMIN_URL=postgresql://admin@host/postgres db/tests/run.sh
# ADMIN_URL needs permission to create databases and roles. Never point it at real data.
set -euo pipefail
: "${ADMIN_URL:?Set ADMIN_URL to a maintenance connection string (scratch server only)}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DB="cairn_test_$$"
# Derive the scratch database URL by swapping the database name in ADMIN_URL.
SCRATCH_URL="$(python3 - "$ADMIN_URL" "$DB" << 'PY'
import sys
from urllib.parse import urlsplit
u = urlsplit(sys.argv[1])
query = ("?" + u.query) if u.query else ""
print(f"{u.scheme}://{u.netloc}/{sys.argv[2]}{query}")
PY
)"
cleanup() { psql "$ADMIN_URL" -X -q -c "DROP DATABASE IF EXISTS $DB WITH (FORCE)" >/dev/null 2>&1 || true; }
trap cleanup EXIT

psql "$ADMIN_URL" -X -q -v ON_ERROR_STOP=1 -c "CREATE DATABASE $DB"
echo "== migrations"
DATABASE_URL="$SCRATCH_URL" "$ROOT/db/apply.sh" context_items_jsonb
echo "== migrations are idempotent"
DATABASE_URL="$SCRATCH_URL" "$ROOT/db/apply.sh" context_items_jsonb | grep -c '^skip' | xargs -I{} echo "{} skipped"
echo "== loader"
DATABASE_URL="$SCRATCH_URL" python3 "$ROOT/tools/load_templates.py" --allow-unreviewed --git-release loader-test
DATABASE_URL="$SCRATCH_URL" python3 "$ROOT/tools/load_templates.py" --allow-unreviewed --git-release loader-test
psql "$SCRATCH_URL" -X -tA -c "SELECT count(*) || ' templates, ' || (SELECT count(*) FROM cairn.template_citations) || ' citations loaded' FROM cairn.task_templates"
echo "== security checks"
psql "$SCRATCH_URL" -X -q -tA -v ON_ERROR_STOP=1 -f "$ROOT/db/tests/verify.sql" | grep -v '^$' | grep -v '^[0-9a-f]\{8\}-[0-9a-f-]\{27\}$'
