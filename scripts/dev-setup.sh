#!/usr/bin/env bash
# Sets up a development environment in one step. Safe to run again.
#   1. a Python virtual environment in .venv with the API, test, lint, and loader dependencies
#   2. a single-node MongoDB replica set (transactions need one), initiated if it isn't yet
#   3. the "cairn" database: collections, validators, indexes, roles, and settings (database/db/apply.py)
#   4. the cairn_api, cairn_jobs, and cairn_loader users, each with one role and a fresh random password
#   5. the task and journey templates (drafts allowed, development only)
#   6. .env, copied from .env.example on the first run, with MONGODB_URI and CAIRN_JOBS_MONGODB_URI filled in
#   7. test logins (database/README.md) and CAIRN_DEV_AUTH_SECRET in .env, so sign-up works without Auth0
#
# Usage: scripts/dev-setup.sh
# CAIRN_ADMIN_MONGODB_URI defaults to the devcontainer's MongoDB (admin:admin@localhost:27017). It needs
# permission to create collections, roles, and users. Never point it at real data.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export CAIRN_ADMIN_MONGODB_URI="${CAIRN_ADMIN_MONGODB_URI:-mongodb://admin:admin@localhost:27017/?replicaSet=rs0}"
export CAIRN_MONGODB_DB="${CAIRN_MONGODB_DB:-cairn}"
PY=.venv/bin/python

echo "== Python environment (.venv)"
[[ -x $PY ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e 'api[test,lint]' -r database/tools/requirements.txt

echo "== MongoDB replica set"
$PY scripts/init_replica_set.py "$CAIRN_ADMIN_MONGODB_URI"

echo "== Database $CAIRN_MONGODB_DB"
$PY database/db/apply.py

echo "== Login users"
login() {
  $PY database/tools/create_login_user.py --user "$1" --role "$2" --generate | tail -n 1
}
APP_URI="$(login cairn_api cairnApp)"
JOBS_URI="$(login cairn_jobs cairnJobs)"
LOADER_URI="$(login cairn_loader cairnLoader)"

echo "== Templates"
CAIRN_LOADER_MONGODB_URI="$LOADER_URI" $PY database/tools/load_templates.py --allow-unreviewed --git-release dev

echo "== .env"
[[ -f .env ]] || cp .env.example .env
# Python, not sed: a URI's "&" means "the whole match" in a sed replacement.
MONGODB_URI="$APP_URI" CAIRN_JOBS_MONGODB_URI="$JOBS_URI" CAIRN_MONGODB_DB="$CAIRN_MONGODB_DB" $PY - <<'PY'
import os
import re
import pathlib
env = pathlib.Path(".env")
text = env.read_text()
for key in ("MONGODB_URI", "CAIRN_JOBS_MONGODB_URI", "CAIRN_MONGODB_DB"):
    line = f"{key}={os.environ[key]}"
    text, n = re.subn(rf"^{key}=.*$", lambda _: line, text, flags=re.MULTILINE)
    if not n:
        text += f"\n{line}\n"
env.write_text(text)
PY

echo "== Test logins (development only)"
# After the login users: create_login_user.py resets cairn_api's roles, and this adds the test login one back.
if $PY database/tools/seed_test_db.py; then
  if ! grep -q '^CAIRN_DEV_AUTH_SECRET=' .env; then
    echo "CAIRN_DEV_AUTH_SECRET=$($PY -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env
  fi
else
  echo "Skipped. Test logins are only added to a database on this machine." >&2
fi

if command -v npm >/dev/null && [[ -f cloudflare/package-lock.json ]]; then
  echo "== Cloudflare Worker dependencies"
  (cd cloudflare && npm ci --no-audit --no-fund --loglevel=error)
fi

echo
echo "Ready. Start the API with:  make run   (then open http://localhost:8000/docs)"
echo "Sign in as a test user with:  make dev-token   (usernames and password in database/README.md)"
