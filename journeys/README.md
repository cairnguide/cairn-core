# Cairn Journey Templates (v0.2.0, draft pending legal review)

Content-as-code templates for every common path a family takes after a death. The files are authored and versioned in the repo, validated in CI, then loaded read-only into the relational database by the template loader.

## How the templates fit together

A case never uses one giant template. It is assembled from four layers, so the same step text is written once and reused everywhere.

| Layer | File(s) | What it decides |
|---|---|---|
| Base journey | `journeys/J-*.json` | The case type. Exactly one applies per case. |
| Modules | `modules/modules.json` | Add, remove, or annotate steps based on case facts (veteran, burial, faith, no will, and so on). |
| Jurisdiction overlays | `jurisdictions/US-*.json` | State-specific content attached to steps, with citations and a verification status. |
| Step library | `steps/step-library.json` | The canonical steps, their comfort-first copy, timing, attorney flags, and citations. |

Supporting files: `sources/sources.json` (every citation, with authority level and retrieval date), `support/support-policy.json` (template behavior in each safety mode), `manifest.json` (file index, key routing, content rules), and `schema/` (JSON Schema for every file, including the case facts input).

## Case types (base journeys)

| ID | Case type | Journey map trailhead |
|---|---|---|
| `J-EXPECTED-HOME-HOSPICE` | Expected death at home with hospice | B, saw it coming |
| `J-EXPECTED-FACILITY` | Death in a hospital, nursing home, or care facility | B, saw it coming |
| `J-SUDDEN-UNEXPECTED` | Sudden or unexpected death, possible medical examiner or coroner review | C, sudden |
| `J-DEATH-ABROAD` | U.S. citizen who died outside the United States | C, sudden |
| `J-DEATH-ABROAD-NONCITIZEN` | Permanent resident or other non-citizen who died outside the United States | C, sudden |

Trailhead A (estate plan already in place) is handled by the `M-HAS-TRUST` module and the will steps, since a plan can exist in any of the four case types.

## Modules

`M-VETERAN`, `M-SSA-BENEFICIARY`, `M-SURVIVING-SPOUSE`, `M-EMPLOYED`, `M-IMMIGRATION`, `M-NO-WILL`, `M-HAS-TRUST`, `M-BURIAL`, `M-CREMATION`, `M-FAITH-COMMUNITY`, `M-FAITH-JEWISH`, `M-FAITH-MUSLIM`, `M-DIED-OUT-OF-STATE`, `M-OWNS-VEHICLE`, `M-OWNS-REAL-ESTATE`.

When a fact is unknown, each module says what to do: `ask` (queue its qualifying question), `ask_if` (ask only when another fact makes it relevant), `include`, or `exclude`. Questions are released one at a time, near the phase they affect, and never in overwhelm or distress modes.

## Jurisdiction coverage

See `COVERAGE.md` for the current matrix of states by key. Regenerate it with `python tools/coverage.py > COVERAGE.md`. New Hampshire, the pilot state, now covers 9 of 11 keys. Florida, California, and New York cover vital records plus the keys families there hit most often. Every overlay stays at legal review pending.

Routing matters. Vital records resolve by the state where the death happened, because certified copies come from that state ([CDC, Where to Write for Vital Records](https://www.cdc.gov/nchs/w2w/index.htm)). Probate, vehicle, and tax keys resolve by the state of residence. When an overlay has no entry, the fallback sets `assistant_must_say_unverified: true`, and the assistant must say it has no verified steps for that state rather than guess.

## Tools

```bash
pip install jsonschema
python tools/validate.py                      # schema, integrity, citations, style lint
python tools/resolve.py examples/cases/case-01-nh-hospice-veteran-jewish.json          # full plan
python tools/resolve.py examples/cases/case-01-nh-hospice-veteran-jewish.json --mvp    # first 28 days only
python tools/check_examples.py                # resolve every example case, fail if any case has no journey
python tools/coverage.py > COVERAGE.md        # regenerate the state coverage matrix
```

The validator fails the build when a step mentions a phone number, dollar amount, form number, or deadline without a citation, when a researched overlay has no source, when any user-facing string contains an em dash or semicolon, when `copy.ask` holds more than one question, or when a step touching an SSN allows chat capture.

`examples/` holds five sample cases and their resolved plans (full and MVP).

## Loading into the database (suggested)

Load each file type into its own read-only table keyed by `(id, version)`: `template_sources`, `template_steps`, `template_journeys`, `template_modules`, `jurisdiction_overlays`. When a case is created, store the resolved plan's step IDs and versions on the case so later template edits never change a family's plan mid-journey. New versions apply only to new cases, or to open cases after an explicit migration.

## Adding a state

1. Copy `jurisdictions/US-DEFAULT.json` to `US-XX.json` and set `id` and `name`.
2. Fill only the keys you have researched from the issuing authority. Delete the rest so they fall back.
3. Add every source to `sources/sources.json`.
4. Register the file in `manifest.json`, then run the validator.
5. Keep `legal_review` at `pending` until the attorney review on [Trello card 23](https://trello.com/c/ljqleuFz) is complete.

## Findings worth acting on

- Board card 14 ("Death certificate ordering assistant") says the flow asks where the decedent resided. Certificates actually come from the state where the death occurred, so the flow should ask for the death location first. The templates route this way.
- Florida issues death certificates with and without cause of death, and its clerks of court will not accept the cause-of-death version for probate ([FL DOH Pasco County application](https://pasco.floridahealth.gov/certificates/_documents/CDA.pdf)). The copy estimator should split copy types for Florida.
- The VA burial allowance time limit comes from a 2014 edition of the form instructions ([VA Form 21P-530, hosted copy](https://www.veterans.ocgov.com/sites/veterans/files/2020-06/VBA-21P-530-ARE%20%28a%29.pdf)). Confirm it against the current 21P-530EZ before launch.

## Known gaps

- No state has researched content yet for trust administration or real estate transfer. Both fall back to attorney routing, which is the right default for real property.
- Florida, California, and New York still fall back for death investigation, cremation authorization, intestacy, state tax, and grief support. New Hampshire still falls back for trust administration, DMV notification, and real estate.
- `grief_support` resolves by the deceased's residence as a stand-in. It should resolve by the user's own location once onboarding collects it.
- Faith modules cover Jewish and Muslim timing needs only. Other traditions receive the general faith community step.
- Fees and dollar limits carry an `as_of` date and should be rechecked on a schedule. California's small estate limit next adjusts April 1, 2028.
- `support/support-policy.json` is a draft that defers mode detection to [Trello card 26](https://trello.com/c/PAA01d2O) and the voice eval set on [card 48](https://trello.com/c/np61DtxY).

See `CHANGELOG.md` for what changed between versions.

Cairn is a guide, not an attorney. Every step that touches legal authority carries an `attorney_flag`, and nothing here ships to real users before legal review.
