# cairn_core

Core functionality for the Cairn app: the HTTP API, the PostgreSQL schema, journey templates, and the voices Cairn speaks in.

Cairn walks a family through the logistics of a death, one step at a time. The data is highly sensitive, so security comes first: the case is the security boundary, enforced in the database with row-level security.

- [Repository layout](#repository-layout)
- [How it runs](#how-it-runs)
- [Run it in GitHub Codespaces](#run-it-in-github-codespaces)
- [Run it on your own machine](#run-it-on-your-own-machine)
- [Deploy to Cloudflare](#deploy-to-cloudflare)
- [Command reference](#command-reference)
- [Troubleshooting](#troubleshooting)

## Repository layout

| Folder | What's in it | More detail |
|---|---|---|
| `api/` | The Cairn API (Python, FastAPI) and the scheduled jobs | [api/README.md](api/README.md) |
| `database/` | PostgreSQL migrations, row-level security, the template loader, and the security test suite | [database/CLAUDE.md](database/CLAUDE.md) |
| `journeys/` | Journey templates and their tools | [journeys/README.md](journeys/README.md) |
| `voices/` | The voices users choose from during onboarding | [voices/README.md](voices/README.md) |
| `auth0/` | Auth0 tenant setup and the post-login Action | [auth0/README.md](auth0/README.md) |
| `cloudflare/` | The Cloudflare Worker that hosts the API | [Deploy to Cloudflare](#deploy-to-cloudflare) |
| `.devcontainer/` | The GitHub Codespaces environment | [Run it in GitHub Codespaces](#run-it-in-github-codespaces) |
| `.github/` | CI, the deploy workflow, and branch protection | [.github/README.md](.github/README.md) |
| `Dockerfile` | The container image Cloudflare runs | |
| `Makefile` | Every command in this README | `make help` |

## How it runs

```
                         Cloudflare
  ┌──────────────────────────────────────────────────────────────┐
  │                                                              │
  │  Client ──HTTPS──▶ Worker "cairn-api" ──▶ CairnApi container ─┼──▶ PostgreSQL 15+
  │                    (cloudflare/src)      (Python API,         │    as cairn_api_login
  │                          │                app role only)      │    (member of cairn_app,
  │                          │                                    │     row-level security)
  │   Cron Triggers ──▶ scheduled() ──▶ CairnJobs container ──────┼──▶ PostgreSQL as OWNER
  │                                     (private, never public)   │    SMTP, Auth0, Apple
  └──────────────────────────────────────────────────────────────┘
```

- The API is unchanged Python. It runs in a [Cloudflare Container](https://developers.cloudflare.com/containers/), built from the `Dockerfile` in this repository. A small Worker in `cloudflare/` is the front door. It receives every request and forwards it to the container.
- Scheduled jobs (outbound email, identity cleanup, held case deletion, draft cleanup, trial status) run in a second container from the same image. [Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/) call the Worker, and the Worker calls that container. It holds the owner connection string and provider secrets. The API container never sees them, and no public request can reach the jobs container.
- PostgreSQL is not on Cloudflare. Use any managed PostgreSQL 15 or newer (for example Neon, Supabase, Crunchy Bridge, or Amazon RDS) that is reachable over the internet with TLS.
- Sign-in is Auth0 ([auth0/README.md](auth0/README.md)). The API only verifies Auth0's signed tokens.

The same image runs anywhere Docker does. `CAIRN_PROCESS=api` (the default) serves the API, and `CAIRN_PROCESS=jobs` serves the jobs.

## Run it in GitHub Codespaces

A Codespace comes with everything installed: Python 3.12, Node 22, the PostgreSQL client, Docker, and a PostgreSQL 16 server. The first start builds the database and writes your settings.

### 1. Create the Codespace

1. On GitHub, open the repository and switch to the branch you want.
2. Select **Code**, then the **Codespaces** tab, then **Create codespace on &lt;branch&gt;**.
   Or, with the GitHub CLI:

   ```bash
   gh codespace create --repo cairnguide/cairn-core --branch main
   ```

3. Wait for the setup to finish. The terminal shows `Ready. Start the API with: make run` when it's done (about 3 to 5 minutes the first time).

The setup ran `scripts/dev-setup.sh` for you. It:

- made a Python virtual environment in `.venv` with the API, test, lint, and loader dependencies
- created a `cairn` database and applied every migration, plus the two optional ones the API needs
- loaded the task and journey templates (drafts allowed, development only)
- created the `cairn_api_login` role with a random password, as a member of `cairn_app`
- copied `.env.example` to `.env` and pointed `DATABASE_URL` at that role
- added two test logins and a `CAIRN_DEV_AUTH_SECRET`, so you can sign up without Auth0 ([database/README.md](database/README.md#test-logins-local-development-only))

If anything failed, run it again. It's safe to repeat:

```bash
make setup
```

### 2. Start the API

```bash
make run
```

Codespaces offers to open port 8000. Open it and add `/docs` to the address to see the Swagger UI. Forwarded ports are private to you by default.

Check it from the terminal:

```bash
curl http://localhost:8000/healthz
```

```bash
curl http://localhost:8000/v1/welcome
```

### 3. Run the tests

```bash
make test
```

```bash
make test-db
```

```bash
make db-check
```

`make test` is the fast suite with no database. `make test-db` adds every use case against a scratch database on the Codespace's PostgreSQL server, and `make db-check` runs the database security suite. Both create a temporary database and drop it afterwards, so they never touch the `cairn` database.

### 4. Try signed-in endpoints

The endpoints under `/v1` other than `/v1/welcome`, `/v1/sign-in-methods`, and `/v1/policies` need an access token.

#### With a test login (no Auth0 needed)

`make setup` added two test logins. The usernames and password are in [database/README.md](database/README.md#test-logins-local-development-only). Get a token and create an account:

```bash
TOKEN=$(make -s dev-token LOGIN=new.user)
```

```bash
curl -X POST http://localhost:8000/v1/registrations -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"time_zone": "America/New_York"}'
```

The answer is `201` and the first onboarding screen. In the Swagger UI, select **Authorize** and paste the token instead. To register `new.user` again, run `make seed-test-db ARGS=--reset`.

Test logins work only while `CAIRN_DEV_AUTH_SECRET` is set. Never set it anywhere real users are. The Cloudflare Worker doesn't pass it to the container.

#### With Auth0

1. Follow [auth0/README.md](auth0/README.md) to set up a development tenant.
2. Put the tenant's values in `.env`: `CAIRN_AUTH0_DOMAIN`, `CAIRN_AUTH0_AUDIENCE`, and `CAIRN_CLAIM_NAMESPACE`.
3. Get a token for a real user, for example with the [Auth0 CLI](https://github.com/auth0/auth0-cli):

   ```bash
   auth0 test token --audience https://api.cairn.example <your-application-client-id>
   ```

4. In the Swagger UI, select **Authorize** and paste the token. Start with `POST /v1/registrations`.

A machine-to-machine token from the Auth0 dashboard's Test tab won't work. It has no user and no sign-in method, so the API refuses it.

The test suite doesn't need Auth0. It replaces sign-in with test headers.

### 5. Try the scheduled jobs

In a second terminal:

```bash
make run-jobs
```

```bash
make job NAME=purge_held_cases
```

`outbound` and `identity_cleanup` answer `"status": "skipped"` until you add SMTP and Auth0 Management settings to `.env`.

### 6. Deploy to Cloudflare from the Codespace

The Codespace has Docker and Node, so you can deploy from it. Follow [Deploy to Cloudflare](#deploy-to-cloudflare) from step 4. Use `npx wrangler login` with the browser link it prints, or set `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` as [Codespaces secrets](https://docs.github.com/en/codespaces/managing-your-codespace/managing-your-account-specific-secrets-for-github-codespaces).

`make cf-dev` (wrangler's local containers) doesn't work well inside a Codespace, because the containers it starts can't reach the Codespace's database. Use `make run` for development there.

### Stopping and deleting

A Codespace stops by itself after 30 minutes idle, and the database keeps its data. Delete it from **github.com/codespaces** when you're done, which deletes the database too.

## Run it on your own machine

You need:

- Python 3.11 or newer (CI uses 3.12)
- PostgreSQL 15 or newer, and its `psql` client
- Docker, to build and test the container image (Docker Desktop, OrbStack, or Colima)
- Node 20 or newer, for the Cloudflare Worker

On macOS with Homebrew:

```bash
brew install python@3.12 postgresql@16 node
```

```bash
brew services start postgresql@16
```

You can use the same Dev Container as Codespaces instead. Open the folder in VS Code and choose **Reopen in Container**.

### Set up

With a local PostgreSQL server, tell the setup script how to reach it as a user that can create databases and roles. Homebrew's server uses your macOS user name with no password:

```bash
ADMIN_URL=postgresql://$(whoami)@localhost:5432/postgres make setup
```

With the PostgreSQL in Docker instead:

```bash
docker run -d --name cairn-postgres -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
```

```bash
make setup
```

Then start the API and open http://localhost:8000/docs:

```bash
make run
```

The tests, jobs, and Auth0 steps are the same as in [Codespaces](#run-it-in-github-codespaces). Pass the same `ADMIN_URL` to `make test-db` and `make db-check` if yours isn't the default.

### Run the container image locally

This is the image Cloudflare runs. It reads `.env` and reaches your database through `host.docker.internal`.

```bash
make docker-build
```

```bash
make docker-run
```

```bash
curl http://localhost:8080/healthz
```

## Deploy to Cloudflare

### What you need

- A Cloudflare account on the **Workers Paid** plan. Containers aren't available on the free plan.
- A managed PostgreSQL 15+ database reachable from the internet over TLS, with an owner role that can create roles (the migrations create `cairn_app` and `cairn_loader`).
- An Auth0 tenant set up with [auth0/README.md](auth0/README.md).
- Docker running on the machine that deploys. `wrangler` builds the image locally and pushes it to Cloudflare's registry. The GitHub Actions deploy workflow does this for you.
- Node 20+ and this repository.

### 1. Create the database

Create a PostgreSQL 15+ database called `cairn` with your provider and copy two things:

- the **owner** connection string, for example `postgresql://owner:…@db.example.com:5432/cairn?sslmode=require`
- confirmation that it's a **direct** connection, not a transaction pooler

The API sets `search_path` and `SET ROLE cairn_app` once per connection, which a transaction-mode pooler (PgBouncer in transaction mode, Neon's `-pooler` host, Supabase's port 6543) would lose between transactions. Use the direct host, or a pooler in session mode. The API keeps its own small pool.

Cloudflare Containers don't have fixed outbound IP addresses, so the database has to accept TLS connections from the internet. Keep strong passwords and require TLS (`sslmode=require` or `verify-full`).

### 2. Apply the migrations and load the templates

From a Codespace or your machine, with the owner connection string in your shell only:

```bash
export OWNER_URL='postgresql://owner:…@db.example.com:5432/cairn?sslmode=require'
```

```bash
DATABASE_URL="$OWNER_URL" database/db/apply.sh context_items_jsonb context_items_read_only
```

```bash
DATABASE_URL="$OWNER_URL" .venv/bin/python database/tools/load_templates.py --git-release "$(git rev-parse --short HEAD)"
```

The loader refuses templates that haven't had counsel review. That's the release gate. For a staging database only, add `--allow-unreviewed`. The current templates are unreviewed drafts (see [api/README.md](api/README.md), decision 8), so production needs that review first.

Later deploys can do this step for you with the [GitHub Actions workflow](#8-optional-deploy-from-github-actions).

### 3. Create the API's login role

The API must never connect as the owner. Create its login role, a member of `cairn_app`, and print its connection string:

```bash
DATABASE_URL="$OWNER_URL" .venv/bin/python database/tools/create_login_role.py --generate
```

The last line is the API's `DATABASE_URL`. Copy it straight into step 6 and don't save it anywhere else. To choose the password yourself, set `CAIRN_LOGIN_PASSWORD` and leave out `--generate`. Running it again changes the password.

### 4. Install the Worker's tools and sign in

```bash
cd cloudflare
```

```bash
npm ci
```

```bash
npx wrangler types
```

```bash
npx wrangler login
```

`wrangler types` generates the Worker's TypeScript types (gitignored). In CI or a Codespace, set `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` instead of logging in. Create the token under **My Profile > API Tokens** with the **Edit Cloudflare Workers** template, and add **Account > Containers > Edit** if it isn't included.

### 5. Set the non-secret settings

Edit the `vars` block in [`cloudflare/wrangler.jsonc`](cloudflare/wrangler.jsonc) and replace the example values with yours:

| Variable | Value |
|---|---|
| `CAIRN_AUTH0_DOMAIN` | Your Auth0 tenant or custom domain |
| `CAIRN_AUTH0_AUDIENCE` | The Cairn API identifier in Auth0 |
| `CAIRN_CLAIM_NAMESPACE` | The same value as the Auth0 Action's `CLAIM_NAMESPACE` secret |
| `CAIRN_AUTH0_EMAIL_CONNECTION` | `email` unless you renamed the passwordless connection |
| `CAIRN_TERMS_VERSION`, `CAIRN_PRIVACY_VERSION` | The current policy versions |
| `CAIRN_PRIVACY_POLICY_URL`, `CAIRN_TERMS_URL`, `CAIRN_JOURNEY_MAP_URL`, `CAIRN_SUPPORT_URL` | Public pages the app links to |
| `CAIRN_AI_PROVIDER_NAME` | The AI provider named on the privacy step [LEGAL REVIEW REQUIRED] |
| `CAIRN_DB_POOL_MIN`, `CAIRN_DB_POOL_MAX` | Connections per API container. Keep `max_instances × CAIRN_DB_POOL_MAX` under your database's connection limit |
| `CAIRN_API_INSTANCES` | How many API containers share traffic. Up to `max_instances` in the `containers` block |
| `CAIRN_SMTP_PORT` | 587 unless your email provider says otherwise |
| `CAIRN_CORS_ORIGINS` | Only for a web client served from another origin: its origins, comma-separated, for example `https://app.cairn.example`. Leave it out for native apps |

Optional settings from [api/README.md](api/README.md), such as `CAIRN_OVERWHELM_SKIP_THRESHOLD`, can be added here too. Every name the containers receive is listed in `API_KEYS` and `JOBS_KEYS` in [`cloudflare/src/index.ts`](cloudflare/src/index.ts).

Settings changed in the Cloudflare dashboard are replaced by this file on the next deploy, so keep them here.

### 6. Set the secrets

Each command asks for the value. Nothing is saved in the repository.

```bash
npx wrangler secret put DATABASE_URL
```

```bash
npx wrangler secret put CAIRN_OWNER_DATABASE_URL
```

`DATABASE_URL` is the login role's connection string from step 3, for the API. `CAIRN_OWNER_DATABASE_URL` is the owner connection string, for the jobs only.

When you've chosen an email provider, add SMTP for the outbound job:

```bash
npx wrangler secret put CAIRN_SMTP_HOST
```

```bash
npx wrangler secret put CAIRN_SMTP_USERNAME
```

```bash
npx wrangler secret put CAIRN_SMTP_PASSWORD
```

```bash
npx wrangler secret put CAIRN_EMAIL_FROM
```

For the identity cleanup job, add the Auth0 Management API client and the Sign in with Apple key ([auth0/README.md](auth0/README.md), step 4):

```bash
npx wrangler secret put CAIRN_AUTH0_MGMT_CLIENT_ID
```

```bash
npx wrangler secret put CAIRN_AUTH0_MGMT_CLIENT_SECRET
```

```bash
npx wrangler secret put CAIRN_APPLE_CLIENT_ID
```

```bash
npx wrangler secret put CAIRN_APPLE_TEAM_ID
```

```bash
npx wrangler secret put CAIRN_APPLE_KEY_ID
```

```bash
npx wrangler secret put CAIRN_APPLE_PRIVATE_KEY < AuthKey_XXXXXXXXXX.p8
```

Until these are set, those two jobs log "skipped" and do nothing. Deletion confirmation emails wait in the queue until SMTP is set.

`wrangler secret put` before the first deploy creates the Worker with only the secret. That's expected. The deploy in the next step fills in the rest.

### 7. Deploy

```bash
npx wrangler deploy
```

This builds the image from the `Dockerfile` for `linux/amd64`, pushes it, and deploys the Worker, both container classes, and the cron triggers. It prints the Worker's address, for example `https://cairn-api.<your-subdomain>.workers.dev`.

Containers take a few minutes to be ready after the first deploy. Then check:

```bash
curl https://cairn-api.<your-subdomain>.workers.dev/healthz
```

```bash
curl https://cairn-api.<your-subdomain>.workers.dev/v1/welcome
```

The first request after a quiet spell starts a container, which takes a few seconds. Each container sleeps after 15 minutes without requests (`sleepAfter` in `cloudflare/src/index.ts`).

To use your own domain, open **Workers & Pages > cairn-api > Settings > Domains & Routes > Add > Custom domain** in the dashboard. The domain's DNS has to be on Cloudflare. Then update the Auth0 application's allowed origins and callback URLs.

### 8. (Optional) Deploy from GitHub Actions

[`.github/workflows/deploy-cloudflare.yml`](.github/workflows/deploy-cloudflare.yml) runs steps 2 and 7 for you: migrations, templates, then `wrangler deploy`.

1. In the repository, open **Settings > Environments > New environment** and name it `cloudflare`. Add required reviewers if you want an approval before each deploy.
2. Add three environment secrets: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and `CAIRN_OWNER_DATABASE_URL`.
3. Keep setting the Worker's own secrets with `wrangler secret put` (step 6). The workflow doesn't touch them.
4. Open **Actions > Deploy to Cloudflare > Run workflow** and choose the branch. Tick **Load templates that haven't had counsel review** for staging only.

Or from the command line:

```bash
gh workflow run deploy-cloudflare.yml --ref main
```

Every pull request already checks that the Worker type-checks and the image builds (`wrangler deploy --dry-run`), so a deploy from a green `main` shouldn't fail on the build.

### Operating it

Stream the Worker's logs, including each job's result:

```bash
npx wrangler tail
```

List deployments and roll back to the previous one:

```bash
npx wrangler deployments list
```

```bash
npx wrangler rollback
```

A rollback changes the Worker and image. It doesn't undo migrations, which only ever move forward.

Run a job by hand to test it. `wrangler dev --test-scheduled` exposes the scheduled handler locally:

```bash
npx wrangler dev --test-scheduled
```

```bash
curl "http://localhost:8787/__scheduled?cron=7+*+*+*+*"
```

The cron schedule and its jobs:

| Cron (UTC) | Jobs | What it does |
|---|---|---|
| `*/5 * * * *` | `outbound` | Deletion confirmations, trial reminders, and chosen notifications by email |
| `*/15 * * * *` | `identity_cleanup` | Deletes Auth0 users and revokes Apple tokens after account deletion |
| `7 * * * *` | `purge_held_cases` | Deletes cases whose 7-day hold has ended (UC-END-13) |
| `30 3 * * *` | `purge_inactive_drafts`, `expire_trials` | Deletes idle drafts (DEC-07). Trial status, for reporting |

To change a schedule, edit `triggers.crons` in `wrangler.jsonc` and `JOBS_BY_CRON` in `src/index.ts` together, then deploy.

### Running the Worker locally with wrangler

`wrangler dev` runs the Worker and starts the container in your local Docker. Copy the example secrets file and point it at your database through `host.docker.internal`:

```bash
cp cloudflare/.dev.vars.example cloudflare/.dev.vars
```

```bash
make cf-dev
```

Then open http://localhost:8787/docs. `cloudflare/.dev.vars` is gitignored.

## Command reference

Run `make help` for this list. Everything the Makefile does is also written out above.

| Command | What it does |
|---|---|
| `make setup` | One-time setup: `.venv`, database, migrations, templates, login role, test logins, `.env` |
| `make seed-test-db` | Add the test logins. `ARGS=--reset` puts the test accounts back to their starting state |
| `make dev-token` | Print a token for a test login while `make run` is up. `LOGIN=new.user` for the one with no account |
| `make run` | The API with reload on port 8000 |
| `make run-jobs` | The jobs service on port 8001 (localhost only) |
| `make job NAME=…` | Trigger one job on the local jobs service |
| `make test` | Fast tests, no database |
| `make test-db` | Every API test on a scratch database |
| `make db-check` | Database security suite on a scratch database |
| `make lint` | The same checks as CI |
| `make openapi` | Regenerate `api/openapi.json` after changing an endpoint |
| `make docker-build`, `make docker-run` | Build and run the container image |
| `make cf-install` | Install the Worker's dependencies |
| `make cf-check` | Type-check the Worker and dry-run the deploy |
| `make cf-dev` | Worker and container locally, with wrangler |
| `make cf-deploy` | Deploy to Cloudflare |
| `make cf-tail` | Stream the deployed Worker's logs |
| `make cf-types` | Generate the Worker's types (`cloudflare/worker-configuration.d.ts`, gitignored) for your editor |

## Troubleshooting

**The API won't start: "Set DATABASE_URL in the environment."** A required setting is missing. Locally, check `.env`. On Cloudflare, check `vars` in `wrangler.jsonc` and `npx wrangler secret list`.

**"Permission denied" or "role … does not exist" from the database.** `DATABASE_URL` must be the login role from `create_login_role.py`, a member of `cairn_app`. Run step 3 again to reset its password.

**Requests fail after a while with a pooler.** The connection string points at a transaction-mode pooler. Use the direct host (see step 1).

**`make setup` says psql is missing.** Install the PostgreSQL client: `sudo apt-get install postgresql-client` on Debian or Ubuntu, `brew install libpq` on macOS (then add `$(brew --prefix libpq)/bin` to your `PATH`).

**`wrangler deploy` fails to build the image.** Docker has to be running. On Apple silicon, the build emulates `linux/amd64`, which is slower but works.

**The first request is slow.** A container was starting. Raise `sleepAfter` in `cloudflare/src/index.ts` to keep containers warm longer.

**`POST /v1/dev/token` returns 404.** `CAIRN_DEV_AUTH_SECRET` isn't set. Run `make setup` again, or add a random value of at least 32 characters to `.env`, then restart `make run`. A 503 means the test logins are missing: run `make seed-test-db`.

**A browser client gets a CORS error.** Add its origin to `CAIRN_CORS_ORIGINS` (in `.env` locally, in `vars` on Cloudflare).

**Every signed-in request returns 401.** The token's audience or issuer doesn't match `CAIRN_AUTH0_AUDIENCE` and `CAIRN_AUTH0_DOMAIN`, or it's a machine-to-machine token. See [Try signed-in endpoints](#4-try-signed-in-endpoints).
