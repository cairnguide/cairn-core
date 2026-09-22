#!/usr/bin/env python3
"""Validate and load content-as-code task templates into the database.

Reads content/tasks/<jurisdiction>/<task_key>.json files, validates them against
content/schema/task-template.schema.json, and inserts new immutable versions into
cairn.task_templates and cairn.template_citations.

Connection: CAIRN_LOADER_DSN (preferred) or DATABASE_URL. The session switches to
the least-privilege cairn_loader role before writing anything.

Release gate (default on, from the data model decision log):
  * counsel_reviewed_at must be set on every template
  * every citation must have last_verified_on set
Use --allow-unreviewed for local development only.
"""
import argparse
import hashlib
import json
import os
import pathlib
import sys

from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parent.parent


def canonical(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_and_validate(content_dir: pathlib.Path, allow_unreviewed: bool):
    schema = json.loads((content_dir / "schema" / "task-template.schema.json").read_text())
    validator = Draft202012Validator(schema)
    errors, templates, seen = [], [], set()

    for path in sorted((content_dir / "tasks").rglob("*.json")):
        rel = path.relative_to(content_dir)
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"{rel}: invalid JSON: {exc}")
            continue

        for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.path)):
            loc = "/".join(str(p) for p in err.path) or "(root)"
            errors.append(f"{rel}: {loc}: {err.message}")
        if any(e.startswith(f"{rel}:") for e in errors):
            continue

        folder = path.parent.name.upper()
        if doc["jurisdiction"] != folder:
            errors.append(f"{rel}: jurisdiction {doc['jurisdiction']} does not match folder {folder}")
        if path.stem != doc["task_key"]:
            errors.append(f"{rel}: file name must equal task_key ({doc['task_key']})")

        week = doc["journey_week"]
        if not ((week - 1) * 7 <= doc["due_offset_days"] <= week * 7):
            errors.append(f"{rel}: due_offset_days {doc['due_offset_days']} falls outside week {week}")

        if not allow_unreviewed:
            if doc["counsel_reviewed_at"] is None:
                errors.append(f"{rel}: counsel_reviewed_at is empty (release gate)")
            for i, c in enumerate(doc["citations"]):
                if c["last_verified_on"] is None:
                    errors.append(f"{rel}: citations/{i}: last_verified_on is empty (release gate)")

        key = (doc["task_key"], doc["version"])
        if key in seen:
            errors.append(f"{rel}: duplicate task_key and version {key}")
        seen.add(key)
        templates.append((rel, doc))

    return templates, errors


def load_into_db(templates, git_release: str, dsn: str):
    import psycopg
    from psycopg.types.json import Jsonb

    inserted = skipped = 0
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute("SET LOCAL ROLE cairn_loader")
            for rel, doc in templates:
                digest = hashlib.sha256(canonical(doc).encode("utf-8")).hexdigest()
                row = conn.execute(
                    "SELECT content_hash FROM cairn.task_templates WHERE task_key = %s AND version = %s",
                    (doc["task_key"], doc["version"]),
                ).fetchone()
                if row:
                    if row[0] == digest:
                        skipped += 1
                        continue
                    raise SystemExit(
                        f"{rel}: task_key {doc['task_key']} version {doc['version']} is already loaded "
                        "with different content. Templates are immutable. Bump the version."
                    )
                newest = conn.execute(
                    "SELECT max(version) FROM cairn.task_templates WHERE task_key = %s",
                    (doc["task_key"],),
                ).fetchone()[0]
                if newest is not None and doc["version"] < newest:
                    raise SystemExit(f"{rel}: version {doc['version']} is older than loaded version {newest}")

                tid = conn.execute(
                    """INSERT INTO cairn.task_templates
                         (task_key, version, title, plain_summary, journey_week, sort_order,
                          due_offset_days, jurisdiction, applies_when, attorney_referral,
                          attorney_referral_note, counsel_reviewed_at, content_hash, git_release)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (
                        doc["task_key"], doc["version"], doc["title"], doc["plain_summary"],
                        doc["journey_week"], doc.get("sort_order", 0), doc["due_offset_days"],
                        doc["jurisdiction"], Jsonb(doc["applies_when"]), doc["attorney_referral"],
                        doc.get("attorney_referral_note"), doc["counsel_reviewed_at"], digest, git_release,
                    ),
                ).fetchone()[0]
                for c in doc["citations"]:
                    conn.execute(
                        """INSERT INTO cairn.template_citations
                             (template_id, authority_name, url, jurisdiction, last_verified_on)
                           VALUES (%s,%s,%s,%s,%s)""",
                        (tid, c["authority_name"], c["url"], c["jurisdiction"], c["last_verified_on"]),
                    )
                conn.execute(
                    "UPDATE cairn.task_templates SET active = false WHERE task_key = %s AND version < %s",
                    (doc["task_key"], doc["version"]),
                )
                inserted += 1
    return inserted, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--content-dir", default=str(ROOT / "content"))
    ap.add_argument("--git-release", help="Git tag or commit being loaded (required unless --dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="Validate only. Do not connect to a database.")
    ap.add_argument("--allow-unreviewed", action="store_true", help="Development only. Skips the release gate.")
    args = ap.parse_args()

    templates, errors = load_and_validate(pathlib.Path(args.content_dir), args.allow_unreviewed)
    if errors:
        print("Validation failed:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    print(f"Validated {len(templates)} template file(s).")
    if args.dry_run:
        return 0

    if not args.git_release:
        print("--git-release is required when loading.", file=sys.stderr)
        return 2
    dsn = os.environ.get("CAIRN_LOADER_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("Set CAIRN_LOADER_DSN or DATABASE_URL.", file=sys.stderr)
        return 2
    inserted, skipped = load_into_db(templates, args.git_release, dsn)
    print(f"Loaded {inserted} new template version(s), {skipped} unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
