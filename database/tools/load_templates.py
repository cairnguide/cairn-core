#!/usr/bin/env python3
"""Validate and load content-as-code task templates and journey selection rules into the database.

Reads content/tasks/<jurisdiction>/<task_key>.json files, validates them against
content/schema/task-template.schema.json, and inserts new immutable versions into
cairn.task_templates and cairn.template_citations.

Also reads content/journeys/*.json (the case creation spec's journey_selection:
base paths, add-ons, and what completed_items mark done), validates them against
content/schema/journey-selection.schema.json, checks that every task they name
has a template, and inserts new immutable versions into cairn.journey_templates.

Connection: CAIRN_LOADER_DSN (preferred) or DATABASE_URL. The session switches to
the least-privilege cairn_loader role before writing anything.

Release gate (default on, from the data model decision log):
  * counsel_reviewed_at must be set on every template
  * every citation must have last_verified_on set
  * counsel_reviewed_at must be set on every journey selection file
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


# Facts that hold lists, for the contains and empty operators.
LIST_FACTS = {"completed_items", "attorney_triggers"}


def _conditions(cond):
    """Every leaf condition in a when clause."""
    for combinator in ("all", "any"):
        if combinator in cond:
            for c in cond[combinator]:
                yield from _conditions(c)
            return
    if "not" in cond:
        yield from _conditions(cond["not"])
        return
    yield cond


def load_and_validate_journeys(content_dir: pathlib.Path, task_keys: set[str], allow_unreviewed: bool):
    """Validate content/journeys/*.json. task_keys is every task_key that has a template."""
    schema = json.loads((content_dir / "schema" / "journey-selection.schema.json").read_text())
    validator = Draft202012Validator(schema)
    errors, journeys, seen = [], [], set()

    for path in sorted((content_dir / "journeys").glob("*.json")):
        rel = path.relative_to(content_dir)
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"{rel}: invalid JSON: {exc}")
            continue
        schema_errors = [f"{rel}: {'/'.join(str(p) for p in e.path) or '(root)'}: {e.message}"
                         for e in sorted(validator.iter_errors(doc), key=lambda e: list(e.path))]
        if schema_errors:
            errors.extend(schema_errors)
            continue

        def check(ok: bool, message: str, rel=rel):
            if not ok:
                errors.append(f"{rel}: {message}")

        paths = doc["paths"]
        for circumstance, key in doc["base_path_by_circumstance"].items():
            check(key in paths, f"base_path_by_circumstance/{circumstance}: no path named {key}")

        named = {k for p in paths.values() for k in p["tasks"]}
        for a in doc["add_ons"]:
            named.update(a.get("add_tasks", []))
            named.update(a.get("replace_tasks", {}).values())
            for n in a.get("add_notes", []):
                check(n in doc["notes"], f"add_ons/{a['id']}: no note named {n}")
            for r in a.get("add_support_resources", []):
                check(r in doc["support_resources"], f"add_ons/{a['id']}: no support resource named {r}")
            for replaced in a.get("replace_tasks", {}):
                check(replaced in named or any(replaced in p["tasks"] for p in paths.values()),
                      f"add_ons/{a['id']}: replaces {replaced}, which no path or add-on includes")
            for leaf in _conditions(a["when"]):
                list_op = leaf["op"] in ("contains", "not_contains", "empty", "not_empty")
                check(list_op == (leaf["fact"] in LIST_FACTS),
                      f"add_ons/{a['id']}: operator {leaf['op']} doesn't fit fact {leaf['fact']}")
        for n in doc["notes"].values():
            if n["attach_to_task"]:
                named.add(n["attach_to_task"])
        mapped = {k for keys in doc["completed_items"].values() for k in keys} | set(doc["check_on_this_when_unsure"])

        for key in sorted(named | mapped):
            check(key in task_keys, f"task {key} has no template in content/tasks")
        for key in sorted(named):
            check(key in doc["task_waypoints"], f"task_waypoints: {key} is missing")
        for key, waypoint in doc["task_waypoints"].items():
            check(waypoint is None or waypoint in doc["waypoints"],
                  f"task_waypoints/{key}: unknown waypoint {waypoint}")

        if not allow_unreviewed:
            check(doc["counsel_reviewed_at"] is not None, "counsel_reviewed_at is empty (release gate)")
        check(doc["version"] not in seen, f"duplicate version {doc['version']}")
        seen.add(doc["version"])
        journeys.append((rel, doc))

    return journeys, errors


def load_into_db(templates, git_release: str, dsn: str, journeys=()):
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
                          attorney_referral_note, why_now, counsel_reviewed_at, content_hash, git_release)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (
                        doc["task_key"], doc["version"], doc["title"], doc["plain_summary"],
                        doc["journey_week"], doc.get("sort_order", 0), doc["due_offset_days"],
                        doc["jurisdiction"], Jsonb(doc["applies_when"]), doc["attorney_referral"],
                        doc.get("attorney_referral_note"), doc.get("why_now"), doc["counsel_reviewed_at"],
                        digest, git_release,
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
            for rel, doc in journeys:
                digest = hashlib.sha256(canonical(doc).encode("utf-8")).hexdigest()
                row = conn.execute("SELECT content_hash FROM cairn.journey_templates WHERE version = %s",
                                   (doc["version"],)).fetchone()
                if row:
                    if row[0] == digest:
                        skipped += 1
                        continue
                    raise SystemExit(f"{rel}: journey selection version {doc['version']} is already loaded "
                                     "with different content. Journey templates are immutable. Bump the version.")
                newest = conn.execute("SELECT max(version) FROM cairn.journey_templates").fetchone()[0]
                if newest is not None and doc["version"] < newest:
                    raise SystemExit(f"{rel}: version {doc['version']} is older than loaded version {newest}")
                conn.execute(
                    """INSERT INTO cairn.journey_templates
                         (version, definition, counsel_reviewed_at, content_hash, git_release)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (doc["version"], Jsonb(doc), doc["counsel_reviewed_at"], digest, git_release),
                )
                conn.execute("UPDATE cairn.journey_templates SET active = false WHERE version < %s",
                             (doc["version"],))
                inserted += 1
    return inserted, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--content-dir", default=str(ROOT / "content"))
    ap.add_argument("--git-release", help="Git tag or commit being loaded (required unless --dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="Validate only. Do not connect to a database.")
    ap.add_argument("--allow-unreviewed", action="store_true", help="Development only. Skips the release gate.")
    args = ap.parse_args()

    content_dir = pathlib.Path(args.content_dir)
    templates, errors = load_and_validate(content_dir, args.allow_unreviewed)
    journeys, journey_errors = load_and_validate_journeys(
        content_dir, {doc["task_key"] for _, doc in templates}, args.allow_unreviewed)
    errors += journey_errors
    if errors:
        print("Validation failed:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    print(f"Validated {len(templates)} template file(s) and {len(journeys)} journey selection file(s).")
    if args.dry_run:
        return 0

    if not args.git_release:
        print("--git-release is required when loading.", file=sys.stderr)
        return 2
    dsn = os.environ.get("CAIRN_LOADER_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("Set CAIRN_LOADER_DSN or DATABASE_URL.", file=sys.stderr)
        return 2
    inserted, skipped = load_into_db(templates, args.git_release, dsn, journeys)
    print(f"Loaded {inserted} new template or journey version(s), {skipped} unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
