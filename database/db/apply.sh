#!/usr/bin/env bash
# Applies pending migrations in order, each in a single transaction.
# Usage:  DATABASE_URL=postgresql://owner@host/dbname db/apply.sh [optional_file_stem ...]
# DATABASE_URL must use the OWNER role. Never the application role. Never commit it.
# Applied files are checksummed. Editing an applied migration is an error.
set -euo pipefail

: "${DATABASE_URL:?Set DATABASE_URL to the owner connection string}"
DIR="$(cd "$(dirname "$0")" && pwd)"
PSQL=(psql "$DATABASE_URL" -X -q -v ON_ERROR_STOP=1)

"${PSQL[@]}" -c "CREATE SCHEMA IF NOT EXISTS cairn;" \
  -c "CREATE TABLE IF NOT EXISTS cairn.schema_migrations (
        filename text PRIMARY KEY, checksum text NOT NULL,
        applied_at timestamptz NOT NULL DEFAULT now());"

files=("$DIR"/migrations/*.sql)
for stem in "$@"; do
  files+=("$DIR/optional/${stem}.sql")
done

for f in "${files[@]}"; do
  [[ -f "$f" ]] || { echo "Missing migration file: $f" >&2; exit 1; }
  name="$(basename "$f")"
  sum="$(sha256sum "$f" | cut -d' ' -f1)"
  existing="$("${PSQL[@]}" -tA -c "SELECT checksum FROM cairn.schema_migrations WHERE filename = '$name'")"
  if [[ -n "$existing" ]]; then
    if [[ "$existing" != "$sum" ]]; then
      echo "ERROR: $name was modified after it was applied. Add a new migration instead." >&2
      exit 1
    fi
    echo "skip   $name"
    continue
  fi
  echo "apply  $name"
  "${PSQL[@]}" --single-transaction -f "$f" \
    -c "INSERT INTO cairn.schema_migrations (filename, checksum) VALUES ('$name', '$sum');"
done
echo "done"
