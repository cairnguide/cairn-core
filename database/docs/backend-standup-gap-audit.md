# Backend stand-up gap audit: hosting the API, registration, and account creation

Date: 2026-09-27. Branch: `cloudflare-hosting`.

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
| G2 | **No test users in a local database.** | `database/tools/seed_test_db.py` and `database/db/dev/test_logins.sql`: a dev-only `cairn_dev` schema (never a migration) with `test.user` (account exists) and `new.user` (login only, so sign-up can be tried). PBKDF2 hashes only, `@example.test` emails only, the app role can call one lookup function and can't read the table, and the script refuses non-local hosts. `--reset` puts both back to their starting state. Credentials are in `database/README.md`. |
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

6. **Managed PostgreSQL 15+** with a direct (not transaction-pooled) TLS connection, then migrations, templates, and `create_login_role.py` (README, "Deploy to Cloudflare", steps 1 to 3). **Blocks production sign-up.**
7. **Cloudflare Workers Paid plan** (Containers need it) and an API token for deploys.
8. **Replace the example values in `cloudflare/wrangler.jsonc` `vars`.** The Auth0 domain and audience, policy URLs, and AI provider name are placeholders. Deploying them unchanged gives an API that refuses every real token. **Blocks production sign-up.**
9. **Set the Worker secrets**: `DATABASE_URL` and `CAIRN_OWNER_DATABASE_URL` at minimum. **Blocks production sign-up.**
10. **Custom domain**, then add it to the Auth0 application's allowed origins and callback URLs. Set `CAIRN_CORS_ORIGINS` if the web client is on another origin.
11. **Backups, restore drills, encryption at rest, and pgaudit** (database/CLAUDE.md, suggested task 5).

### Abuse protection

12. **Rate limiting on sign-up and sign-in.** The API has none. Auth0 has brute-force and suspicious-IP protection for its own endpoints, but `POST /v1/registrations` and the rest of the API rely on Cloudflare. Add a Cloudflare rate limiting rule (WAF) for `/v1/*` before launch.
13. **Bot detection on sign-up** in Auth0 (Attack Protection) if abuse appears.

### Legal and product decisions

14. **Policy text and versions.** `CAIRN_TERMS_VERSION`, `CAIRN_PRIVACY_VERSION`, and the policy URLs must point to reviewed documents. Registration refuses any other version, so these gate sign-up. **[LEGAL REVIEW REQUIRED]**
15. **AI provider named on the privacy step** (`CAIRN_AI_PROVIDER_NAME`, UC-REG-07). **[LEGAL REVIEW REQUIRED]**
16. **`draft_copy` in the registration copy** (personality samples, deletion wording, link labels) needs product and legal review (api/README.md).
17. **Template counsel review.** The loader refuses unreviewed templates in production. Not needed to register, but needed before a case can start a journey.
18. **Retention periods** for accounts that never finish onboarding (UC-REG-10) and never create a case (UC-REG-13), used by `purge_stale_accounts`. **[LEGAL REVIEW REQUIRED]**
19. **UC-REG-14 crisis plan** (Trello card 26) is not written.
20. **Email provider** for the outbound job (deletion confirmations, trial reminders).

### Features not built

21. **Linking a second sign-in method** to an existing account (UC-REG-05). Today the API returns 409 `account_exists` and points to the first method.
22. **Subscriptions and billing.** After the trial, accounts become read-only with no way to subscribe (database/CLAUDE.md, open question 9).

### Found while checking

23. **Intermittent failure in `test_uc15_sensitive_numbers_are_redacted_before_storage_logs_and_reply`** (`api/tests/test_case_creation.py`). It failed once in about five full runs and never in 25 runs on its own. It asserts that short digit fragments such as `4111` never appear in database dumps or DEBUG logs, and those dumps and logs contain random UUIDs, whose hex can contain the same digits. It isn't related to this change, but it can fail a pull request at random. Match the fragments against the whole sensitive values, or strip UUIDs before checking.
24. **Local `psql` is required** by `make setup` and `make db-check`. macOS doesn't ship it. The README's troubleshooting covers `brew install libpq`, and the Dev Container installs it.
