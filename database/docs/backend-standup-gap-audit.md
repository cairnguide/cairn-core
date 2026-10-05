# Backend stand-up gap audit: hosting the API, registration, and account creation

> **2026-10-05:** `account-creation-review-gap-audit.md` closes items 20 (email provider) and 21 (linking a second sign-in method). Every email now goes through Twilio SendGrid, and text messages have a Twilio sender, still not offered (OPEN-05).

Date: 2026-09-27. Branch: `cloudflare-hosting`.

Updated 2026-10-03, when this branch took in the move from PostgreSQL to MongoDB (`main`, pull request 8). The check below was run against PostgreSQL. The test logins were moved to MongoDB, and the items that named PostgreSQL now say what replaces them.

The question: what is missing to stand up this backend so it hosts the API and a person can register and create a new account (`POST /v1/registrations`, then onboarding UC-REG-07 to UC-REG-12)?

## How this was checked

The setup was run for real, not only read. A PostgreSQL 16 server was started in Docker, then:

1. `make setup` (migrations, optional migrations, templates, login role, `.env`). It worked.
2. `make run`, then the public endpoints (`/healthz`, `/v1/welcome`). They worked.
3. `POST /v1/registrations`. It could not be called: every request was 401, and nothing local could produce a token the API accepts.
4. The full API suite (`make test-db`, 401 tests), the database security suite (`make db-check`), and lint. All passed.

So the code for registration is complete and tested. The gaps are in standing it up and signing in.

## Gaps closed in this change

| # | Gap | What was built |
|---|---|---|
| G1 | **No way to register locally without an Auth0 tenant.** The API only accepts Auth0 RS256 tokens, and the tests replace sign-in with test headers that only exist inside pytest. A developer couldn't call `POST /v1/registrations` at all. | `api/cairn_api/dev_auth.py`: `POST /v1/dev/token` exchanges a test username and password for an HS256 token with the same claims as the Auth0 Action. The API accepts it alongside Auth0 tokens. It is off unless `CAIRN_DEV_AUTH_SECRET` (32+ characters) is set, answers 404 when off, is left out of `openapi.json`, and the Cloudflare Worker never forwards the secret. |
| G2 | **No test users in a local database.** | `database/tools/seed_test_db.py`: a dev-only `cairn_dev` database (never in `db/schema.py`) with `test.user` (account exists) and `new.user` (login only, so sign-up can be tried). PBKDF2 hashes only, `@example.test` emails only, `cairnApp` has no access to it, the local API user gets one extra role that can only read the logins, and the script refuses non-local hosts. `--reset` puts both back to their starting state. Credentials are in `database/README.md`. |
| G3 | **Setup didn't prepare sign-in.** | `scripts/dev-setup.sh` now seeds the test logins and writes a random `CAIRN_DEV_AUTH_SECRET` to `.env` once. New `make seed-test-db` and `make dev-token`. |
| G4 | **No CORS.** A web client on another origin (the Auth0 README allows a single-page application) couldn't call registration from a browser. | `CAIRN_CORS_ORIGINS`, a comma-separated allow list. Off by default, a wildcard is refused, credentials are not allowed (the API uses bearer tokens). Forwarded by the Worker. |
| G5 | **The Auth0 README contradicted the default connection.** Its environment example set `CAIRN_AUTH0_EMAIL_CONNECTION=Username-Password-Authentication`, but the default and the spec (D-10) use the passwordless `email` connection. | Corrected to `email`. |

Tests: `api/tests/test_dev_auth.py` covers the dev token round trip, refusal of wrong secrets, issuers, audiences, and expired tokens, delegation of RS256 tokens to Auth0, the hash format shared by the seed and the API, settings validation, the non-local host refusal, CORS, and the full seed, token, register (201), resume (200), and reset flow against a real database.

## Work still to do

None of these can be done in code in this repository. Items marked **blocks production sign-up** must be done before a real person can register on a deployed API.

### Identity (Auth0)

1. **Create the Auth0 tenant** (`auth0/README.md`, steps 1 to 5): the API with the Cairn audience, the application, and the post-login Action with its `CLAIM_NAMESPACE` secret. **Blocks production sign-up.**
2. **Google connection** with Cairn's own OAuth client ID and secret, not Auth0's development keys. **Blocks Google sign-up.**
3. **Apple connection**: Services ID, Team ID, Key ID, and private key, plus the Private Email Relay domain registration with SPF and DKIM. **Blocks Apple sign-up.**
4. **Passwordless email connection** in link mode, 900-second expiry, sending from Cairn's own domain through a custom SMTP provider. **Blocks email sign-up.**
5. **Management API client** for account deletion (`identity_cleanup` job). Not needed to register, but needed before deletion can finish.

### Database and hosting

6. **MongoDB Atlas (M10 or larger)**, or another MongoDB 7.0+ replica set reachable over TLS, then the roles, users, schema, and templates (README, "Deploy to Cloudflare", steps 1 to 3). **Blocks production sign-up.**
7. **Cloudflare Workers Paid plan** (Containers need it) and an API token for deploys.
8. **Replace the example values in `cloudflare/wrangler.jsonc` `vars`.** The Auth0 domain and audience, policy URLs, and AI provider name are placeholders. Deploying them unchanged gives an API that refuses every real token. **Blocks production sign-up.**
9. **Set the Worker secrets**: `MONGODB_URI` and `CAIRN_JOBS_MONGODB_URI` at minimum. **Blocks production sign-up.**
10. **Custom domain**, then add it to the Auth0 application's allowed origins and callback URLs. Set `CAIRN_CORS_ORIGINS` if the web client is on another origin.
11. **Backups, restore drills, encryption at rest, and Atlas database auditing** (database/CLAUDE.md, suggested task 5).

### Abuse protection

12. **Rate limiting on sign-up and sign-in.** The API has none. Auth0 has brute-force and suspicious-IP protection for its own endpoints, but `POST /v1/registrations` and the rest of the API rely on Cloudflare. Add a Cloudflare rate limiting rule (WAF) for `/v1/*` before launch.
13. **Bot detection on sign-up** in Auth0 (Attack Protection) if abuse appears.

### Legal and product decisions

14. **Policy text and versions.** `CAIRN_TERMS_VERSION`, `CAIRN_PRIVACY_VERSION`, and the policy URLs must point to reviewed documents. Registration refuses any other version, so these gate sign-up. **[LEGAL REVIEW REQUIRED]**
15. **AI provider named on the privacy step** (`CAIRN_AI_PROVIDER_NAME`, UC-REG-07). **[LEGAL REVIEW REQUIRED]**
16. **`draft_copy` in the registration copy** (personality samples, deletion wording, link labels) needs product and legal review (api/README.md).
17. **Template counsel review.** The loader refuses unreviewed templates in production. Not needed to register, but needed before a case can start a journey.
18. **Retention periods** for accounts that never create a case (UC-REG-13), used by `purge_stale_accounts`. **[LEGAL REVIEW REQUIRED]** Accounts that never finish onboarding (UC-REG-10) are deleted after 90 days (decided 2026-10-05).
19. **UC-REG-14 crisis plan** (Trello card 26) is not written.
20. **Email provider** for the outbound job (deletion confirmations, trial reminders).

### Features not built

21. **Linking a second sign-in method** to an existing account (UC-REG-05). Today the API returns 409 `account_exists` and points to the first method.
22. **Subscriptions and billing.** After the trial, accounts become read-only with no way to subscribe (database/CLAUDE.md, open question 9).

### Found while checking

23. **Intermittent failure in `test_uc15_sensitive_numbers_are_redacted_before_storage_logs_and_reply`** (`api/tests/test_case_creation.py`). It failed once in about five full runs and never in 25 runs on its own. It asserts that short digit fragments such as `4111` never appear in database dumps or DEBUG logs, and those dumps and logs contain random UUIDs, whose hex can contain the same digits. It isn't related to this change, but it can fail a pull request at random. Match the fragments against the whole sensitive values, or strip UUIDs before checking. Resolved 2026-09-27: the cause is confirmed. The reply, the dumps, and the log all carry random material. The reply holds the case id and three timestamps, and its checks include short fragments such as `078`. Rerandomizing the ids and the microseconds in a real run's output 200,000 times failed the old checks in about 2.5% of runs, mostly on the reply. That makes one failure in five runs unlucky but plausible. An id and a timestamp deliberately injected with `4111`, `078`, and `1120` in them failed the old checks and pass the new ones. The test now removes UUIDs and ISO timestamps from the reply, the dumps, and the log before it checks, and keeps every fragment it checked before. The storage and log checks also look for the SSN without dashes. The same simulation now fails 0 runs, and 10 full `make test-db` runs passed. With redaction disabled for the SSN on purpose, the test still fails on the reply and, with the reply check skipped, on the stored rows. After the move to MongoDB the test failed in every full run, on `main` too: at DEBUG, pymongo logs every command with random request, operation, and connection ids, ObjectIds, binary ids, and clock values. The test now removes those driver fields by name as well. The logged commands keep every stored value, so a leak written to the database still shows up in the log check. 8 full runs on MongoDB 8.0 passed, and with SSN redaction broken on purpose the stored rows and the log each fail the test on their own.
24. **Local `psql` is required** by `make setup` and `make db-check`. macOS doesn't ship it. The README's troubleshooting covers `brew install libpq`, and the Dev Container installs it. Resolved 2026-10-03 by the move to MongoDB: neither command uses `psql` now.
