#!/usr/bin/env python3
"""Resolve every example case and regenerate the example plans. Fails if any case cannot resolve.

Usage: python tools/check_examples.py
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from resolve import resolve
ROOT = Path(__file__).resolve().parent.parent
bad = 0
for case in sorted((ROOT / "examples/cases").glob("*.json")):
    facts = json.loads(case.read_text())
    try:
        for mvp in (False, True):
            plan = resolve(facts, mvp_only=mvp)
            out = ROOT / "examples" / f"plan-{case.stem}{'.mvp' if mvp else ''}.json"
            out.write_text(json.dumps(plan, indent=2, ensure_ascii=False))
        print(f"ok   {case.stem}: {plan['journey']['id']}, {plan['step_count']} MVP steps")
    except SystemExit as e:
        bad += 1; print(f"FAIL {case.stem}: {e}")
sys.exit(1 if bad else 0)
