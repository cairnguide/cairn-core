"""Builds the system prompt for one Cairn turn and calls the model.

Runs on the server only. The API key never ships to the phone or browser.
Order matters for prompt caching: shared core first (cached across every
user and voice), then the voice (cached across everyone on that voice),
then per-case context (changes every turn, not cached).
"""
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
MANIFEST = yaml.safe_load((ROOT / "manifest.yaml").read_text())
CORE = (ROOT / MANIFEST["core"]).read_text()
VOICES = {v["id"]: (ROOT / v["file"]).read_text() for v in MANIFEST["voices"]}


def build_system(voice_id, safety_mode, case_context, citations):
    voice = VOICES.get(voice_id, VOICES[MANIFEST["default_voice"]])
    return [
        {"type": "text", "text": CORE, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"<voice>\n{voice}\n</voice>",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": (
            f"<safety_mode>{safety_mode}</safety_mode>\n"
            f"<case_context>\n{case_context}\n</case_context>\n"
            f"<citations>\n{citations}\n</citations>"
        )},
    ]


def cairn_reply(voice_id, safety_mode, case_context, citations, history):
    # case_context must already be redacted: no SSNs, account numbers, or PINs.
    import anthropic  # imported here so build_system can be used and tested without the SDK or a key

    response = anthropic.Anthropic().messages.create(
        model="claude-sonnet-5",  # set from config, not hard-coded, in production
        max_tokens=600,
        system=build_system(voice_id, safety_mode, case_context, citations),
        messages=history,
    )
    return "".join(b.text for b in response.content if b.type == "text")
