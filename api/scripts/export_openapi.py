"""Write the OpenAPI contract to api/openapi.json.

Run after changing any endpoint or schema:  python api/scripts/export_openapi.py
CI regenerates it and fails if the committed file is out of date.
"""
import json
import pathlib

from cairn_api.main import app

OUT = pathlib.Path(__file__).resolve().parents[1] / "openapi.json"

if __name__ == "__main__":
    OUT.write_text(json.dumps(app.openapi(), indent=2) + "\n")
    print(f"Wrote {OUT}")
