#!/usr/bin/env python3
"""Print a Markdown coverage matrix of jurisdiction keys by state overlay.

Usage: python tools/coverage.py > COVERAGE.md
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
m = json.loads((ROOT / "manifest.json").read_text())
keys = list(m["jurisdiction_keys"])
ov = [json.loads((ROOT / p).read_text()) for p in m["files"]["jurisdictions"]]
ov = [o for o in ov if o["id"] != "US-DEFAULT"]
mark = {"researched": "Researched", "general_only": "General", "not_verified": "Not verified"}
print("# Jurisdiction coverage\n")
print(
    "Cells show the overlay status. A dash means the key falls back to US-DEFAULT. Where that fallback is "
    "marked not_verified, the assistant must say it has no verified steps for the state.\n"
)
print("| Key | Resolved by | " + " | ".join(o["id"] for o in ov) + " |")
print("|---|---|" + "---|" * len(ov))
for k in keys:
    cells = [mark[o["keys"][k]["status"]] if k in o["keys"] else "-" for o in ov]
    print(f"| {k} | {m['jurisdiction_keys'][k]['resolve_by']} | " + " | ".join(cells) + " |")
print("\nAll overlays: legal review " + ", ".join(f"{o['id']} {o['legal_review']}" for o in ov) + ".")
