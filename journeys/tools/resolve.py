#!/usr/bin/env python3
"""Resolve a case into a concrete Cairn journey plan.

Reference implementation of the resolution order in manifest.json. The app's
template loader can port this logic, or call it at build time to pre-compute plans.

  1. Pick the one base journey whose applies_when is true.
  2. Apply every module whose applies_when is true. For unknown facts, follow
     the module's when_unknown rule and queue qualifying questions (asked one at a time).
  3. Apply journey step_overrides, then module step_overrides.
  4. Attach jurisdiction content for each step key, resolved by death_state or
     residence_state, falling back to US-DEFAULT with a not_verified flag.
  5. Compute target dates from date_of_death when it is known.

Usage: python tools/resolve.py case.json [--mvp] > plan.json
"""
import json, sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
load = lambda rel: json.loads((ROOT / rel).read_text())
UNKNOWN = object()

def get(facts, name):
    v = facts.get(name, None)
    return UNKNOWN if v is None else v

def evaluate(cond, facts):
    """Three-valued logic. Returns True, False, or UNKNOWN."""
    if "all" in cond:
        vals = [evaluate(c, facts) for c in cond["all"]]
        if any(v is False for v in vals): return False
        return UNKNOWN if any(v is UNKNOWN for v in vals) else True
    if "any" in cond:
        vals = [evaluate(c, facts) for c in cond["any"]]
        if any(v is True for v in vals): return True
        return UNKNOWN if any(v is UNKNOWN for v in vals) else False
    if "not" in cond:
        v = evaluate(cond["not"], facts)
        return v if v is UNKNOWN else (not v)
    val, op = get(facts, cond["fact"]), cond["op"]
    if op == "exists": return val is not UNKNOWN
    if val is UNKNOWN: return UNKNOWN
    target = cond.get("value")
    if op in ("eq_fact", "ne_fact"):
        other = get(facts, target)
        if other is UNKNOWN: return UNKNOWN
        return (val == other) if op == "eq_fact" else (val != other)
    if op == "eq": return val == target
    if op == "ne": return val != target
    if op == "in": return val in (target or [])
    if op == "not_in": return val not in (target or [])
    raise ValueError(f"unknown op {op}")

def resolve(facts, mvp_only=False):
    manifest = load("manifest.json")
    F = manifest["files"]
    sources = {s["id"]: s for s in load(F["sources"])["sources"]}
    lib = load(F["steps"])
    steps = {s["id"]: s for s in lib["steps"]}
    phase_titles = {p["id"]: p["title"] for p in lib["phases"]}
    modules = load(F["modules"])["modules"]
    support = load(F["support_policy"])
    overlays = {}
    for p in F["jurisdictions"]:
        j = load(p); overlays[j["id"]] = j

    journeys = [load(p) for p in F["journeys"]]
    matches = [j for j in journeys if evaluate(j["applies_when"], facts) is True]
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one base journey, matched {[j['id'] for j in matches]}")
    journey = matches[0]

    plan = {p["phase"]: list(p["steps"]) for p in journey["phases"]}
    overrides = {}
    for k, v in journey["step_overrides"].items():
        o = {kk: vv for kk, vv in v.items() if kk != "note"}
        o["notes"] = [v["note"]] if "note" in v else []
        overrides[k] = o
    applied, questions, flags, support_resources = [], [], set(), []

    for m in modules:
        state = evaluate(m["applies_when"], facts)
        include = state is True
        if state is UNKNOWN:
            rule = m["when_unknown"]
            if rule == "include": include = True
            elif rule == "ask": questions.append({"module": m["id"], "question": m["qualifying_question"]})
            elif rule == "ask_if" and evaluate(m["ask_if"], facts) is True:
                questions.append({"module": m["id"], "question": m["qualifying_question"]})
        if not include: continue
        applied.append(m["id"]); flags.update(m.get("flags", []))
        support_resources += m["support_resources"]
        for a in m["add_steps"]:
            lst = plan.setdefault(a["phase"], [])
            if a["step_id"] in lst: continue
            if a.get("position") == "first": lst.insert(0, a["step_id"])
            elif a.get("after") in lst: lst.insert(lst.index(a["after"]) + 1, a["step_id"])
            else: lst.append(a["step_id"])
        for sid in m["remove_steps"]:
            for lst in plan.values():
                if sid in lst: lst.remove(sid)
        for sid, o in m["step_overrides"].items():
            cur = overrides.setdefault(sid, {"notes": []})
            if "note" in o: cur["notes"].append(o["note"])
            for k in ("priority", "timing_shift_days"):
                if k in o: cur[k] = o[k]

    dod = date.fromisoformat(facts["date_of_death"]) if facts.get("date_of_death") else None
    mvp_windows = set(manifest["mvp_windows"])
    keyreg = manifest["jurisdiction_keys"]
    cited = set(journey["citations"])

    def juris_for(key):
        by = keyreg[key]["resolve_by"]
        st = facts.get(by)
        if not st:
            return None  # no U.S. state applies (for example a death abroad), so no state content is attached
        ov = overlays.get(f"US-{st}") if st else None
        if ov and key in ov["keys"]:
            e = ov["keys"][key]; jid = ov["id"]; review = ov["legal_review"]
        else:
            e = overlays["US-DEFAULT"]["keys"][key]; jid = "US-DEFAULT"; review = overlays["US-DEFAULT"]["legal_review"]
        cited.update(e["source_ids"])
        return {"key": key, "resolved_by": by, "state": st, "overlay": jid, "status": e["status"],
                "legal_review": review, "summary": e["summary"], "fields": e["fields"],
                "source_ids": e["source_ids"], "attorney_flag": e.get("attorney_flag"),
                "assistant_must_say_unverified": e["status"] == "not_verified"}

    out_phases = []
    for pid in ["W0", "W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8"]:
        items = []
        for sid in plan.get(pid, []):
            s = steps[sid]
            if mvp_only and s["timing"]["window"] not in mvp_windows: continue
            o = overrides.get(sid, {})
            days = s["timing"].get("target_days_after_death")
            if days is not None: days += o.get("timing_shift_days", 0)
            cited.update(s["citations"]); [cited.update(d["source_ids"]) for d in s["deadlines"]]
            items.append({
                "step_id": sid, "version": s["version"], "title": s["title"], "copy": s["copy"], "how_to": s["how_to"],
                "notes": o.get("notes", []), "priority": o.get("priority", "normal"),
                "window": s["timing"]["window"], "mvp_first_28_days": s["timing"]["window"] in mvp_windows,
                "target_date": (dod + timedelta(days=days)).isoformat() if (dod and days is not None) else None,
                "deadlines": s["deadlines"], "certified_copy_needed": s["certified_copy_needed"],
                "depends_on": s["depends_on"], "sensitive_data": s["sensitive_data"],
                "attorney_flag": s["attorney_flag"], "support": s["support"], "tracker": s["tracker"],
                "citations": s["citations"], "citation_status": s["citation_status"],
                "jurisdiction": [j for j in (juris_for(k) for k in s["jurisdiction_keys"]) if j],
                "status": "not_started"})
        if items: out_phases.append({"phase": pid, "title": phase_titles[pid], "steps": items})

    for t in journey["support_touchpoints"]: cited.update(t["resource_ids"])
    cited.update(support_resources); cited.update(support["source_ids"])
    present = {s["step_id"] for p in out_phases for s in p["steps"]}
    subs = journey.get("dependency_substitutions", {})
    for p in out_phases:
        for s in p["steps"]:
            deps = [subs.get(d, d) for d in s["depends_on"]]
            s["depends_on"] = [d for d in dict.fromkeys(deps) if d in present]
    ids = [i for p in out_phases for i in (s["step_id"] for s in p["steps"])]
    return {
        "plan_type": "mvp_first_28_days" if mvp_only else "full",
        "journey": {"id": journey["id"], "version": journey["version"], "title": journey["title"], "status": journey["status"]},
        "modules_applied": applied, "flags": sorted(flags),
        "qualifying_questions": questions,
        "question_policy": "Ask one qualifying question at a time, only when the related phase is near, and never in overwhelm, acute_distress, or risk_of_harm modes.",
        "estimated_certified_copies": sum(1 for p in out_phases for s in p["steps"] if s["certified_copy_needed"]) + 2,
        "phases": out_phases,
        "support_touchpoints": journey["support_touchpoints"],
        "support_resources": sorted(set(support_resources)),
        "support_policy_ref": support["id"],
        "step_count": len(ids),
        "sources": [sources[i] for i in sorted(cited) if i in sources],
    }

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    facts = json.loads(Path(args[0]).read_text())
    print(json.dumps(resolve(facts, mvp_only="--mvp" in sys.argv), indent=2, ensure_ascii=False))
