# Cairn personas

core.md holds the rules every voice must follow (comfort-first rules, citations,
attorney referrals, sensitive data, distress protocol). The files in voices/
change tone only. manifest.yaml lists the voices and their onboarding labels.
assemble_prompt.py shows how the server stitches them together per turn.

Status: draft 0.1. The reference responses in each voice file show tone, not
verified procedure. Any factual claim in them must pass the same legal content
review as the journey templates before real users see it.
