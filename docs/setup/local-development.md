# Local development

You can run all of Cairn without any outside account. Auth0 is replaced by local test logins, email by "skipped" (or an SMTP catcher), and Stripe by "payments aren't available" (or test mode keys).

The full walkthrough is in the root [README](../../README.md#run-it-in-github-codespaces). This page is the short version.

## GitHub Codespaces (recommended)

1. **Code > Codespaces > Create codespace on &lt;branch&gt;**, or run `gh codespace create --repo cairnguide/cairn-core --branch main`.
2. Wait for `Ready. Start the API with: make run` (3 to 5 minutes the first time). `scripts/dev-setup.sh` has already built `.venv`, started MongoDB 8.0 as a replica set with authentication, applied the schema, created the three users, loaded the templates, written `.env`, and added two test logins.
3. Run the API and open the forwarded port with `/docs`:

```bash
make run
```

## Your own machine

You need Python 3.11+, Node 20+, and Docker. Start MongoDB the way CI does (the command is in the [README](../../README.md#set-up)), then:

```bash
make setup
```

```bash
make run
```

Or open the folder in VS Code and choose **Reopen in Container** to use the Codespaces environment locally.

## Signing in locally

```bash
TOKEN=$(make -s dev-token LOGIN=new.user)
```

```bash
curl -X POST http://localhost:8000/v1/registrations -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"time_zone": "America/New_York"}'
```

The test logins (`test.user`, `new.user`) live in a separate `cairn_dev` database and work only while `CAIRN_DEV_AUTH_SECRET` is set. How they're kept out of production is in [database/README.md](../../database/README.md#how-it-stays-out-of-production) and [Security model](../security/overview.md#development-sign-in).

## Trying the integrations locally

| Integration | How |
|---|---|
| Auth0 | Put a development tenant's values in `.env` ([3. Auth0](03-auth0.md)) and use `auth0 test token` |
| Stripe | Test mode keys in `.env`, and `stripe listen --forward-to localhost:8000/v1/stripe/webhook` |
| Email | `CAIRN_EMAIL_PROVIDER=smtp` with an SMTP catcher, or a SendGrid key |
| Jobs | `make run-jobs` in a second terminal, then `make job NAME=outbound` |
| The Worker and containers | `make cf-dev` with `cloudflare/.dev.vars` (not inside a Codespace) |

Never put production values in `.env`. It's gitignored, but it's still a plain-text file on your disk.

## Tests

| Command | What |
|---|---|
| `make test` | Fast suite, no database |
| `make test-db` | Everything, on a scratch database, connected as a `cairnApp` user |
| `make db-check` | The data security suite only |
| `make lint` | What CI runs |

Run `make help` for every command.
