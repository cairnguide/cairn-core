# cairn-db-mvp

Database scripts for the Cairn MVP: PostgreSQL migrations, row-level security, a content-as-code template loader, and a security test suite.

Start with [CLAUDE.md](CLAUDE.md) for context, decisions, commands, and open questions.

Quick check (needs a scratch PostgreSQL 15+ server):

```bash
pip install -r tools/requirements.txt
ADMIN_URL=postgresql://admin@localhost/postgres db/tests/run.sh
```

## Test logins (local development only)

`make setup` adds two test logins to your local `cairn` database, so you can sign up and sign in without an Auth0 tenant. Cairn never stores real passwords. Auth0 owns sign-in. These are fake credentials for fake accounts, and they only work while the API runs with `CAIRN_DEV_AUTH_SECRET` set, which `make setup` writes to `.env`.

| Username | Password | Email | What it's for |
|---|---|---|---|
| `test.user` | `cairn-local-test-password` | `test.user@example.test` | Has an account already, at the start of onboarding. `POST /v1/registrations` resumes it (200). |
| `new.user` | `cairn-local-test-password` | `new.user@example.test` | Has a login but no account. `POST /v1/registrations` creates the account (201), so you can try the whole sign-up flow. |

Usernames aren't case sensitive. To use a different password, set `CAIRN_TEST_PASSWORD` when you seed and when you run `make dev-token`.

### Sign in and register

With the API running (`make run`), get a token and create the account:

```bash
TOKEN=$(make -s dev-token LOGIN=new.user)
```

```bash
curl -X POST http://localhost:8000/v1/registrations -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"time_zone": "America/New_York"}'
```

Or in the Swagger UI at http://localhost:8000/docs, select **Authorize** and paste the token from `make dev-token`. The token lasts 8 hours.

Without `make`, call the token endpoint directly:

```bash
curl -X POST http://localhost:8000/v1/dev/token -H "Content-Type: application/json" -d '{"username": "new.user", "password": "cairn-local-test-password"}'
```

### Reset or re-create them

Put both test accounts back to their starting state, deleting anything they created, so `new.user` can register again:

```bash
make seed-test-db ARGS=--reset
```

`make seed-test-db` on its own adds the logins without touching existing accounts. Both read the owner connection string from `CAIRN_OWNER_DATABASE_URL` in `.env`. By hand:

```bash
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/cairn python3 tools/seed_test_db.py --reset
```

### How it stays out of production

- The logins live in a separate `cairn_dev` schema created by [`db/dev/test_logins.sql`](db/dev/test_logins.sql). That file is not a migration. `db/apply.sh` never reads `db/dev/`, so a real database has no `cairn_dev` schema.
- The table stores a PBKDF2 hash, never the password. The app role can't read the table. It can only call `cairn_dev.test_login(username)`, and every email must end in `@example.test`.
- `tools/seed_test_db.py` refuses any database host other than this machine unless you pass `--allow-remote`.
- `POST /v1/dev/token` answers 404 unless `CAIRN_DEV_AUTH_SECRET` is set, and it's left out of the OpenAPI contract. The Cloudflare Worker never passes that setting to the container.
