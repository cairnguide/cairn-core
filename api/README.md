# Cairn API (MVP)

HTTP API for the MVP use cases in `database/docs/cairn-mvp-use-cases.md`, built on the schema in `database/db/migrations`. FastAPI generates the contract from the request and response models, which are the only way data enters or leaves the service.

- Swagger UI: `/docs` when running. Static contract: [`openapi.json`](openapi.json) (OpenAPI 3.1).
- Requests reject unknown fields, trim whitespace, and validate against the same rules as the database (state codes, date order, lengths, allowed values).
- Errors use RFC 9457 problem details (`application/problem+json`) and never echo submitted values or database messages.
- Any response that moves a conversation forward includes a `next_step` with one prompt, so clients ask one question at a time.

## Use case map

| Use case | Endpoint |
|---|---|
| UC-1 to UC-4, registration for each persona | `GET /v1/sign-in-methods` (Google, Apple, email), then `POST /v1/registrations` (plus `GET /v1/policies`, `GET /v1/me`) |
| UC-5, start a case and record identity | `POST /v1/cases`, then `PATCH /v1/cases/{id}/deceased` for edits |
| UC-6, death event | `PUT /v1/cases/{id}/death-event` |
| UC-7, veteran and will status | `PATCH /v1/cases/{id}/estate-flags` (one answer at a time, `skip` saves `unknown`) |
| UC-8, fiduciary single session | `POST /v1/cases` with `death_event`, `estate_flags`, and `start_journey` |
| UC-9, the four-week journey | `POST` and `GET /v1/cases/{id}/journey` |
| UC-10, order death certificates | `GET /v1/cases/{id}/tasks/{task_id}`, `POST .../certificate-order` |
| UC-11, notify a bank | `POST .../tasks/{task_id}/institution-notices` |
| UC-12, step back from tasks | `POST /v1/cases/{id}/journey/pause` and `/resume` |
| UC-13, status across the case | `GET /v1/cases/{id}/status` |
| Any task status change or snooze | `PATCH /v1/cases/{id}/tasks/{task_id}` |

## Running

```bash
python3 -m venv .venv && .venv/bin/pip install -e 'api[test]'
export DATABASE_URL=postgresql://cairn_api_login@host/cairn   # a login role that is a member of cairn_app
export CAIRN_AUTH0_DOMAIN=... CAIRN_AUTH0_AUDIENCE=... CAIRN_CLAIM_NAMESPACE=...   # see auth0/README.md
export CAIRN_TERMS_VERSION=... CAIRN_PRIVACY_VERSION=...
.venv/bin/uvicorn cairn_api.main:app --app-dir api
```

The database needs migrations 0001 to 0007 **and** the optional `context_items_jsonb` migration (`db/apply.sh context_items_jsonb`). UC-10 and UC-11 store their records in `context_items` (`CERT_ORDER` and `BANK_NOTICES`).

## Tests

```bash
cd api
../.venv/bin/pytest                                          # contract tests, no database
CAIRN_TEST_ADMIN_URL=postgresql://admin@localhost/postgres ../.venv/bin/pytest   # plus the use case suite
```

The use case suite creates a scratch database, applies the migrations, loads the templates, runs every use case as `cairn_app` under row-level security, and then drops the database.

## Security choices

- Every request runs in one transaction with `app.user_id` set through the transaction-local `set_config`, and each pooled connection runs `SET search_path = cairn, pg_temp` and `SET ROLE cairn_app`.
- Accounts are created with Google, Apple, or an email address, all through Auth0 (see [auth0/README.md](../auth0/README.md)). Auth0 handles passwords, passkeys, and email confirmation. The API only verifies Auth0's signed token and refuses any other sign-in connection. The email address and sign-in method come from the token, never from the request body, and registration is refused until `email_verified` is true.
- Every write adds an `audit_events` row in the same transaction. Reads of the deceased's details (`GET /v1/cases/{id}`, `/status`) are audited too.
- The service logs only the SQL error class and constraint name. It never logs names, dates, or free text.
- `ssn_last4` is never read or returned. Cause of death is not collected. Pausing has no reason field.
- A missing case and a case you can't access both return the same 403, so case IDs can't be probed.

## Decisions to confirm with the product owner

1. **Accounts aren't linked across sign-in methods.** An email that already has an account gets a 409 saying which method to use. Merging accounts with Auth0 account linking is a separate decision.
2. **Fiduciary flag (UC-4, UC-8).** The spec says not to add a user-level column without sign-off. The persona picked at registration only shapes the response (`language_profile`). It's stored per case as `case_members.relationship`.
3. **POA role confirmation (UC-3).** `confirm_current_role` offers `named_executor`, `next_of_kin`, and `not_sure` as client routing values only. The note that POA authority ends at death is marked `legal_review_required`. [LEGAL REVIEW REQUIRED]
4. **`context_items` stays in Postgres** (open question 1). The optional migration is required as described above.
5. **Task categories and kinds** (UC-11, UC-13) are keyed by `task_key` in `cairn_api/journey.py`. They should become a `category` field in the template content schema.
6. **Pause length** defaults to 30 days (maximum 90), because `tasks_paused_until` needs an end time. When it expires, the tasks come back on their own.
7. **Case list for fiduciaries** is not built, as UC-8 asks. It should be tracked as its own ticket.
8. **New templates** `order_death_certificates`, `choose_funeral_provider`, and `notify_banks` in `database/content/tasks/us/` are unreviewed drafts, like the existing samples. They need counsel review and `last_verified_on` dates before release.
