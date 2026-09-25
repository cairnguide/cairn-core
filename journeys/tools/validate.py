#!/usr/bin/env python3
"""Validate the Cairn journey template package.

Checks
  1. JSON Schema for every file.
  2. Referential integrity: step, source, module, and phase references resolve.
  3. Citation policy: steps with phone numbers, dollar amounts, form numbers, or
     deadlines must cite a source. Researched overlay keys must cite a source.
  4. Style lint for user-facing strings: no em dashes, no semicolons, one question in copy.ask.

Usage: python tools/validate.py [package_root]
Exit code 0 when clean, 1 when any error is found.
"""
import json, re, sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:
    sys.exit("pip install jsonschema")

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
errors, warnings = [], []

def load(rel):
    return json.loads((ROOT / rel).read_text())

def schema_check(doc, schema_file, label):
    schema = load(f"schema/{schema_file}")
    for e in Draft202012Validator(schema).iter_errors(doc):
        errors.append(f"[schema] {label}: {'/'.join(map(str, e.path))} {e.message}")

manifest = load("manifest.json")
F = manifest["files"]
sources = load(F["sources"]); schema_check(sources, "source.schema.json", F["sources"])
steps_doc = load(F["steps"]); schema_check(steps_doc, "step-library.schema.json", F["steps"])
modules_doc = load(F["modules"]); schema_check(modules_doc, "modules.schema.json", F["modules"])
support = load(F["support_policy"]); schema_check(support, "support-policy.schema.json", F["support_policy"])
journeys = {p: load(p) for p in F["journeys"]}
for p, j in journeys.items(): schema_check(j, "journey.schema.json", p)
juris = {p: load(p) for p in F["jurisdictions"]}
for p, j in juris.items(): schema_check(j, "jurisdiction.schema.json", p)

SRC = {s["id"] for s in sources["sources"]}
if len(SRC) != len(sources["sources"]): errors.append("[integrity] duplicate source ids")
STEPS = {s["id"]: s for s in steps_doc["steps"]}
if len(STEPS) != len(steps_doc["steps"]): errors.append("[integrity] duplicate step ids")
KEYS = set(manifest["jurisdiction_keys"])

def need_src(ids, where):
    for i in ids:
        if i not in SRC: errors.append(f"[integrity] {where}: unknown source {i}")

def need_step(i, where):
    if i not in STEPS: errors.append(f"[integrity] {where}: unknown step {i}")

# Steps
CLAIM = re.compile(r"(\$\d|\b\d{3}-\d{3}-\d{4}\b|\bForm\s+[0-9A-Z-]{2,}|\bdial\s+988|\b988\b)", re.I)
used_steps = set()
for s in STEPS.values():
    need_src(s["citations"], s["id"])
    for d in s["deadlines"]: need_src(d["source_ids"], s["id"] + " deadline")
    for dep in s["depends_on"]: need_step(dep, s["id"] + " depends_on")
    for k in s["jurisdiction_keys"]:
        if k not in KEYS: errors.append(f"[integrity] {s['id']}: jurisdiction key {k} not registered in manifest")
    text = " ".join([s["copy"]["summary"], *s["how_to"]])
    if CLAIM.search(text) and not s["citations"]:
        errors.append(f"[citation] {s['id']}: contains a phone number, amount, or form number but cites nothing")
    if s["deadlines"] and not s["citations"]:
        errors.append(f"[citation] {s['id']}: has deadlines but no step citations")
    if s["sensitive_data"]["fields"] and s["sensitive_data"]["chat_capture"] != "never":
        errors.append(f"[privacy] {s['id']}: sensitive fields must set chat_capture to never")

# Journeys
for p, j in journeys.items():
    need_src(j["citations"], p)
    for ph in j["phases"]:
        for sid in ph["steps"]:
            need_step(sid, p); used_steps.add(sid)
            if sid in STEPS and STEPS[sid]["phase"] != ph["phase"]:
                warnings.append(f"[phase] {p}: {sid} is phase {STEPS[sid]['phase']} but placed in {ph['phase']}")
    for sid in j["step_overrides"]: need_step(sid, p + " override")
    for a, b in j.get("dependency_substitutions", {}).items(): need_step(a, p + " substitution"); need_step(b, p + " substitution")
    for t in j["support_touchpoints"]: need_src(t["resource_ids"], p + " touchpoint")

# Modules
for m in modules_doc["modules"]:
    need_src(m["citations"] + m["support_resources"], m["id"])
    for a in m["add_steps"]:
        need_step(a["step_id"], m["id"]); used_steps.add(a["step_id"])
        if a["step_id"] in STEPS and STEPS[a["step_id"]]["phase"] != a["phase"]:
            warnings.append(f"[phase] {m['id']}: {a['step_id']} is phase {STEPS[a['step_id']]['phase']} but added to {a['phase']}")
        if "after" in a: need_step(a["after"], m["id"] + " after")
    for sid in m["step_overrides"]: need_step(sid, m["id"] + " override")
    if m["when_unknown"] in ("ask", "ask_if") and not m.get("qualifying_question"):
        errors.append(f"[module] {m['id']}: when_unknown={m['when_unknown']} needs a qualifying_question")
    if m["when_unknown"] == "ask_if" and "ask_if" not in m:
        errors.append(f"[module] {m['id']}: when_unknown=ask_if needs an ask_if condition")

for sid in STEPS:
    if sid not in used_steps: warnings.append(f"[orphan] {sid} is not used by any journey or module")

# Jurisdictions
default = juris.get("jurisdictions/US-DEFAULT.json")
if not default: errors.append("[integrity] US-DEFAULT overlay missing")
else:
    for k in KEYS:
        if k not in default["keys"]: errors.append(f"[fallback] US-DEFAULT has no entry for key {k}")
for p, j in juris.items():
    for k, e in j["keys"].items():
        if k not in KEYS: errors.append(f"[integrity] {p}: key {k} not registered")
        need_src(e["source_ids"], f"{p}:{k}")
        if e["status"] in ("researched", "general_only") and not e["source_ids"]:
            errors.append(f"[citation] {p}:{k}: status {e['status']} requires source_ids")
need_src(support["source_ids"], "support-policy")

# Style lint over user-facing strings
def strings(obj, path=""):
    if isinstance(obj, str): yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("product_notes", "internal_notes", "url", "$schema", "$id", "pattern"): continue
            yield from strings(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj): yield from strings(v, f"{path}[{i}]")

docs = {F["steps"]: steps_doc, F["modules"]: modules_doc, F["support_policy"]: support, **journeys, **juris}
for name, doc in docs.items():
    for path, text in strings(doc):
        if "\u2014" in text: errors.append(f"[style] {name}{path}: em dash")
        if ";" in text: errors.append(f"[style] {name}{path}: semicolon")
for s in STEPS.values():
    if s["copy"]["ask"].count("?") != 1: errors.append(f"[style] {s['id']}: copy.ask must contain exactly one question")

for w_ in warnings: print("WARN ", w_)
for e in errors: print("ERROR", e)
print(f"\n{len(STEPS)} steps, {len(journeys)} journeys, {len(modules_doc['modules'])} modules, "
      f"{len(juris)} jurisdictions, {len(SRC)} sources. {len(errors)} errors, {len(warnings)} warnings.")
sys.exit(1 if errors else 0)
