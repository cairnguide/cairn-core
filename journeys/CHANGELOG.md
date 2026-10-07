# Changelog

## Unreleased (2026-10-06)

Added
- `answer_facts` on a module (schema `modules.schema.json`) and `resolve.answer_question`, so one answer can set more than one fact. M-EMPLOYED uses it: a no sets `was_employed` and `had_employer_plan` to false, so the question isn't queued again (UC-JEF-03, UC-JSU-03). No step, journey, or template version changes, so resolved plans are the same as 0.2.0.
- The journey use case specs v1.1.0 for J-EXPECTED-FACILITY and J-SUDDEN-UNEXPECTED run as tests (`api/tests/test_journey_use_cases.py`).

## 0.2.0 (2026-09-25)

Added
- Journey `J-DEATH-ABROAD-NONCITIZEN` for permanent residents and other non-citizens who die outside the United States, with steps `S-W0-ABROAD-NONCITIZEN` and `S-W1-FOREIGN-DC`. This closes the case that previously failed to resolve.
- Jurisdiction key `grief_support`, attached to `S-W8-GRIEF-SUPPORT`, with a 988 fallback in US-DEFAULT.
- New Hampshire: death investigation (Office of the Chief Medical Examiner, RSA 611-B:11), cremation authorization (RSA 325-A:17, 325-A:18, 5-C:71, 290:16, 290:17), state tax (NH DRA), and grief support (NH OCME resource list).
- Florida: summary administration ($75,000 limit or more than 2 years since death).
- California: small estate affidavit ($208,850 for deaths on or after April 1, 2025, 40-day wait).
- New York: vital records (separate NYC and state systems) and DMV notification, including the rule that plates must be surrendered before cancelling liability insurance.
- `S-W4-OTHER-INSURANCE` now carries the `dmv_notification` key so the New York plate rule surfaces where families cancel insurance.
- Tools: `check_examples.py` (resolves all sample cases) and `coverage.py` (writes COVERAGE.md). Sample case 05.
- 19 new sources, 62 total.

## 0.1.0 (2026-09-25)

- Initial package: 56 steps, 4 base journeys, 15 modules, overlays for NH, FL, CA, NY, and US-DEFAULT, support policy draft, schemas, validator, resolver.
