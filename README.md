# cairn_core

Core functionality for the Cairn app: the HTTP API, the MongoDB schema, journey templates, and the voices Cairn speaks in.

Cairn walks a family through the logistics of a death, one step at a time. The data is highly sensitive, so security comes first: the case is the security boundary, enforced in one data-access layer that every request goes through (`api/cairn_api/store.py`), with MongoDB validators and least-privilege roles as the backstop.

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
| `database/` | The MongoDB schema (validators, indexes, roles), the template loader, and the user and data-moving tools | [database/README.md](database/README.md), [database/CLAUDE.md](database/CLAUDE.md) |
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
  │  Client ──HTTPS──▶ Worker "cairn-api" ──▶ CairnApi container ─┼──▶ MongoDB Atlas
  │                    (cloudflare/src)      (Python API,         │    as cairn_api
  │                          │                app user only)      │    (cairnApp role only,
  │                          │                                    │     validators on)
  │   Cron Triggers ──▶ scheduled() ──▶ CairnJobs container ──────┼──▶ MongoDB as cairn_jobs
  │                                     (private, never public)   │    Twilio, Auth0, Apple, Stripe
  └──────────────────────────────────────────────────────────────┘
```

- The API is unchanged Python. It runs in a [Cloudflare Container](https://developers.cloudflare.com/containers/), built from the `Dockerfile` in this repository. A small Worker in `cloudflare/` is the front door. It receives every request and forwards it to the container.
- Scheduled jobs (outbound email through Twilio and browser notifications, identity cleanup, held case deletion, draft cleanup, unfinished sign-up cleanup, access after the free days, and Stripe retries) run in a second container from the same image. [Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/) call the Worker, and the Worker calls that container. It holds the jobs user's connection string and provider secrets. The API container never sees them, and no public request can reach the jobs container.
- MongoDB is not on Cloudflare. Use [MongoDB Atlas](https://www.mongodb.com/docs/atlas/) (M10 or larger), or any MongoDB 7.0 or newer replica set reachable over the internet with TLS. Every request is one multi-document transaction, which needs a replica set.
- Sign-in is Auth0 ([auth0/README.md](auth0/README.md)). The API only verifies Auth0's signed tokens, and signs a session out after 5 minutes with no activity (D-20).
- Subscriptions are Stripe ([Stripe](#stripe-subscriptions)). Payment details are entered on Stripe's own pages, and the API changes access only from Stripe's signed webhook events.

The same image runs anywhere Docker does. `CAIRN_PROCESS=api` (the default) serves the API, and `CAIRN_PROCESS=jobs` serves the jobs.

## Run it in GitHub Codespaces

A Codespace comes with everything installed: Python 3.12, Node 22, Docker, and a MongoDB 8.0 server with authentication on. The first start builds the database and writes your settings.

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
- made the MongoDB server a single-node replica set, so transactions work
- created a `cairn` database with its collections, validators, indexes, roles, and settings (`database/db/apply.py`)
- created the `cairn_api`, `cairn_jobs`, and `cairn_loader` users, each with one role and a random password
- loaded the task and journey templates as `cairn_loader` (drafts allowed, development only)
- copied `.env.example` to `.env` and pointed `MONGODB_URI` and `CAIRN_JOBS_MONGODB_URI` at those users
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

`make test` is the fast suite with no database. `make test-db` adds every use case and the data security suite against a scratch database on the Codespace's MongoDB server, with the API connected as a `cairnApp` user. `make db-check` runs the data security suite on its own. Both create a temporary database with its own users and roles and drop them afterwards, so they never touch the `cairn` database.

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

`outbound` and `identity_cleanup` answer `"status": "skipped"` until you add the Twilio and Auth0 Management settings to `.env` (see [Twilio](#twilio-email-and-text-messages)). `purge_stale_accounts` runs with its 90-day default.

### 6. Deploy to Cloudflare from the Codespace

The Codespace has Docker and Node, so you can deploy from it. Follow [Deploy to Cloudflare](#deploy-to-cloudflare) from step 4. Use `npx wrangler login` with the browser link it prints, or set `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` as [Codespaces secrets](https://docs.github.com/en/codespaces/managing-your-codespace/managing-your-account-specific-secrets-for-github-codespaces).

`make cf-dev` (wrangler's local containers) doesn't work well inside a Codespace, because the containers it starts can't reach the Codespace's database. Use `make run` for development there.

### Stopping and deleting

A Codespace stops by itself after 30 minutes idle, and the database keeps its data. Delete it from **github.com/codespaces** when you're done, which deletes the database too.

## Run it on your own machine

You need:

- Python 3.11 or newer (CI uses 3.12)
- Docker, for MongoDB and to build and test the container image (Docker Desktop, OrbStack, or Colima)
- Node 20 or newer, for the Cloudflare Worker

On macOS with Homebrew:

```bash
brew install python@3.12 node
```

You can use the same Dev Container as Codespaces instead. Open the folder in VS Code and choose **Reopen in Container**.

### Set up

Start MongoDB 8.0 in Docker as a replica set with authentication, the same way CI does. The `admin` password is for this throwaway local server only:

```bash
docker run -d --name cairn-mongo -p 27017:27017 -e MONGO_INITDB_ROOT_USERNAME=admin -e MONGO_INITDB_ROOT_PASSWORD=admin \
  --entrypoint bash mongo:8.0 -c 'head -c 756 /dev/urandom | base64 > /tmp/keyfile && chmod 400 /tmp/keyfile && chown 999:999 /tmp/keyfile && exec docker-entrypoint.sh mongod --replSet rs0 --keyFile /tmp/keyfile --bind_ip_all'
```

```bash
make setup
```

`make setup` initiates the replica set the first time. With a different MongoDB, pass an administrator URI: `make setup MONGO_ADMIN_URI='mongodb://...'`.

Then start the API and open http://localhost:8000/docs:

```bash
make run
```

The tests, jobs, and Auth0 steps are the same as in [Codespaces](#run-it-in-github-codespaces). Pass the same `MONGO_ADMIN_URI` to `make test-db` and `make db-check` if yours isn't the default.

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
- A MongoDB Atlas cluster (M10 or larger), or another MongoDB 7.0+ replica set reachable from the internet over TLS, and an administrator login for setup.
- An Auth0 tenant set up with [auth0/README.md](auth0/README.md).
- Docker running on the machine that deploys. `wrangler` builds the image locally and pushes it to Cloudflare's registry. The GitHub Actions deploy workflow does this for you.
- Node 20+ and this repository.

### 1. Create the database, roles, and users

Follow [database/README.md](database/README.md), "Atlas setup": create the cluster, add the `cairnApp`, `cairnJobs`, and `cairnLoader` custom roles (`python3 database/db/apply.py --print-roles` prints them), and create one user per role:

- `cairn_api` with `cairnApp`, for the API (`MONGODB_URI`)
- `cairn_jobs` with `cairnJobs`, for the jobs (`CAIRN_JOBS_MONGODB_URI`)
- `cairn_loader` with `cairnLoader`, for loading templates

Atlas refuses `createRole` and `createUser` from a driver, which is why roles and users are made in Atlas ([Unsupported Commands](https://www.mongodb.com/docs/atlas/unsupported-commands/)). On a self-managed replica set, `database/db/apply.py` creates the roles and `database/tools/create_login_user.py` creates the users.

Cloudflare Containers don't have fixed outbound IP addresses, so the cluster's IP access list has to accept the containers' connections ([IP Access List](https://www.mongodb.com/docs/atlas/security/ip-access-list/)). Keep strong passwords and TLS on. Whether to add a fixed-egress proxy first is an open decision (`database/CLAUDE.md`, open question 12).

### 2. Apply the schema and load the templates

From a Codespace or your machine, with the connection strings in your shell only:

```bash
export CAIRN_ADMIN_MONGODB_URI='mongodb+srv://admin:…@cluster.example.mongodb.net/'
```

```bash
.venv/bin/python database/db/apply.py --db cairn --skip-roles
```

```bash
CAIRN_LOADER_MONGODB_URI='mongodb+srv://cairn_loader:…@cluster.example.mongodb.net/' \
  .venv/bin/python database/tools/load_templates.py --git-release "$(git rev-parse --short HEAD)"
```

Leave out `--skip-roles` on a self-managed deployment, where `apply.py` can create the roles itself. If the environment already has data in PostgreSQL, move it now with `database/tools/move_from_postgres.py` (see [database/README.md](database/README.md)).

The loader refuses templates that haven't had counsel review. That's the release gate. For a staging database only, add `--allow-unreviewed`. The current templates are unreviewed drafts (see [api/README.md](api/README.md), decision 8), so production needs that review first.

Later deploys can do this step for you with the [GitHub Actions workflow](#8-optional-deploy-from-github-actions).

### 3. Copy the API and jobs connection strings

The API must never connect as an administrator. Its `MONGODB_URI` is the `cairn_api` user's connection string, and the jobs' `CAIRN_JOBS_MONGODB_URI` is the `cairn_jobs` user's. Copy them straight into step 6 and don't save them anywhere else.

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
| `CAIRN_EMAIL_PROVIDER` | `twilio` (Twilio SendGrid). `smtp` is for local development only |
| `CAIRN_PENDING_ACCOUNT_RETENTION_DAYS` | Days before an unfinished sign-up is deleted. `90`, the decided period (D-2026-10-05-R1). Leave it out to use 90 |
| `CAIRN_NO_CASE_ACCOUNT_RETENTION_DAYS` | Leave out. The period for finished accounts with no case isn't decided [LEGAL REVIEW REQUIRED] |
| `CAIRN_CORS_ORIGINS` | Only for a web client served from another origin: its origins, comma-separated, for example `https://app.cairn.example`. Leave it out for native apps |
| `CAIRN_APP_URL` | The web app's address. Stripe sends the user back here after Checkout and the customer portal |
| `CAIRN_SUBSCRIPTION_PRICE_CENTS` | `1499`. The one price, shown and charged (D-04). Every price in the copy must match it, or the API refuses to start |
| `CAIRN_STRIPE_PRODUCT_ID` | The Cairn subscription product in Stripe, for example `prod_...` |
| `CAIRN_VAPID_PUBLIC_KEY` | The public half of the browser notification key. Leave it out to hide browser notifications |
| `CAIRN_VAPID_SUBJECT` | A `mailto:` or `https:` contact the browser push services can reach |
| `CAIRN_PRICE_CHANGE_EFFECTIVE_DATE`, `CAIRN_PRICE_CHANGE_NEW_PRICE` | Only while a price change is scheduled (UC-SUB-18), for example `2027-03-01` and `$16.99` |

Optional settings from [api/README.md](api/README.md), such as `CAIRN_OVERWHELM_SKIP_THRESHOLD`, can be added here too. Every name the containers receive is listed in `API_KEYS` and `JOBS_KEYS` in [`cloudflare/src/index.ts`](cloudflare/src/index.ts).

Settings changed in the Cloudflare dashboard are replaced by this file on the next deploy, so keep them here.

### 6. Set the secrets

Each command asks for the value. Nothing is saved in the repository.

```bash
npx wrangler secret put MONGODB_URI
```

```bash
npx wrangler secret put CAIRN_JOBS_MONGODB_URI
```

`MONGODB_URI` is the `cairn_api` user's connection string from step 3, for the API. `CAIRN_JOBS_MONGODB_URI` is the `cairn_jobs` user's, for the jobs only. The database name comes from `CAIRN_MONGODB_DB` in `wrangler.jsonc` (`cairn`).

For the outbound job, add Twilio (see [Twilio](#twilio-email-and-text-messages)). Email needs the Twilio SendGrid API key and the sender:

```bash
npx wrangler secret put TWILIO_SENDGRID_API_KEY
```

```bash
npx wrangler secret put CAIRN_EMAIL_FROM
```

The Account SID and Auth Token are for text messages and Verify. Text messages aren't in the MVP (OPEN-05, decided 2026-10-05), so nothing sends a text to users. These can wait, but setting them now does no harm:

```bash
npx wrangler secret put TWILIO_ACCOUNT_SID
```

```bash
npx wrangler secret put TWILIO_AUTH_TOKEN
```

```bash
npx wrangler secret put TWILIO_FROM_NUMBER
```

For the identity cleanup job, add the Auth0 Management API client and the Sign in with Apple key ([auth0/README.md](auth0/README.md), step 5):

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

Until these are set, those two jobs log "skipped" and do nothing. Deletion confirmation emails wait in the queue until the Twilio SendGrid key is set.

Stripe and browser notifications (see [Stripe](#stripe-subscriptions)):

```bash
npx wrangler secret put CAIRN_STRIPE_SECRET_KEY        # the restricted or secret key, sk_live_... in production
npx wrangler secret put CAIRN_STRIPE_WEBHOOK_SECRET    # whsec_..., from the webhook endpoint
npx wrangler secret put CAIRN_VAPID_PRIVATE_KEY < vapid-private.pem   # the browser notification key (P-256, PEM)
```

The API container gets the Stripe key and the webhook secret. The jobs container gets the Stripe key and the VAPID private key. Without the Stripe key, the subscription routes answer that payments aren't available and nothing is charged.

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

[`.github/workflows/deploy-cloudflare.yml`](.github/workflows/deploy-cloudflare.yml) runs steps 2 and 7 for you: the schema, templates, then `wrangler deploy`.

Deploys run only by hand, from this workflow or `npx wrangler deploy`. Don't connect the repository to Cloudflare **Workers Builds** (Workers & Pages > Settings > Builds). It would deploy every merge to `main` without applying the schema or loading templates first. It also fails today, because it expects the Worker name in `cloudflare/wrangler.jsonc` (`cairn-api`) to match the dashboard project's name. The `cairn-core` project was disconnected from Workers Builds on 2026-10-05 for this reason, before anything was deployed.

1. In the repository, open **Settings > Environments > New environment** and name it `cloudflare`. Add required reviewers if you want an approval before each deploy.
2. Add four environment secrets: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CAIRN_ADMIN_MONGODB_URI` (an administrator, for `apply.py`), and `CAIRN_LOADER_MONGODB_URI` (the `cairn_loader` user). On Atlas, also add the environment variable `CAIRN_MANAGE_ROLES` set to `false`, because Atlas manages the roles.
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

A rollback changes the Worker and image. It doesn't undo schema changes: validators and indexes stay as the newest `apply.py` left them, so keep schema changes backward compatible for one release.

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
| `*/5 * * * *` | `outbound` | Confirmations, the trial-ending note, the reminders users chose, check-ins, break-ending notices, and the yearly subscription reminder, by email through Twilio SendGrid and by browser notification |
| `*/15 * * * *` | `identity_cleanup`, `process_stripe_events`, `retry_cancellations` | Deletes Auth0 users and revokes Apple tokens after account deletion. Finishes Stripe events and cancellations that couldn't be finished at the time |
| `7 * * * *` | `purge_held_cases`, `settle_trial_clocks`, `sync_early_subscriptions` | Deletes cases whose 7-day hold has ended (UC-END-13). Starts the free days again after a care rest ends on its own (DEC-26-01). Keeps an early subscriber's first charge at the free days' end (UC-SUB-06) |
| `30 3 * * *` | `purge_inactive_drafts`, `expire_trials`, `purge_stale_accounts`, `price_change_notices` | Deletes idle drafts (DEC-07). Keeps `users.access` current (D-19). Deletes sign-ups still unfinished after 90 days (UC-REG-10). Price change notices, only when one is scheduled (UC-SUB-18) |

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

## Stripe (subscriptions)

Cairn is free for 28 days from the first journey, then $14.99 a month for the whole account (D-04). The subscription spec is `database/docs/cairn-subscription-use-cases-v33.json`. The code is [`api/cairn_api/stripe_client.py`](api/cairn_api/stripe_client.py) (the Stripe calls, through `httpx`, so there's no Stripe SDK), [`subscription.py`](api/cairn_api/subscription.py) (status mapping and event handling), and [`routers/subscription.py`](api/cairn_api/routers/subscription.py).

- **Checkout.** Subscriptions start on Stripe Checkout, Stripe's hosted page. Cairn sends Stripe the account id and the sign-in email only (SUB-D-10), and never receives a card, bank account, or billing address (SUB-D-01). The price is sent with each session from `CAIRN_SUBSCRIPTION_PRICE_CENTS`, tax inclusive, with automatic tax on (SUB-D-12).
- **Access follows Stripe's events, never the browser.** Register a webhook endpoint at `https://<your host>/v1/stripe/webhook` for these events: `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.paid`, `invoice.payment_failed`, `invoice.payment_action_required`, `customer.updated`, `charge.refunded`, and `charge.dispute.created`. Put its signing secret in `CAIRN_STRIPE_WEBHOOK_SECRET`. Each event is verified, recorded once by id (ids, type, and times only, never the payload), and applied from the subscription's current state in Stripe (UC-SUB-22).
- **In the Stripe Dashboard before launch:** create the product (its tax code chosen with an accountant), turn on Stripe Tax and register where needed, configure the customer portal for payment methods and invoices with no retention offers or coupons (SUB-D-04), and turn on Stripe's failed-payment emails (SUB-D-08). Cairn sends no failed-payment email of its own.
- **Owner alerts.** A dispute, a reversed charge, an event for an unknown customer, or a subscription that can't be cancelled before an account is deleted logs an `owner alert` line at ERROR from `cairn_api.subscription` or `cairn_api.account`, with ids only. Route those to whoever owns billing (`npx wrangler tail` shows them, and a log drain can page on them).
- **Testing.** Tests use a mocked Stripe client and fixture events signed with a test secret. For end-to-end checks, use a Stripe sandbox with test clocks and `stripe listen --forward-to localhost:8000/v1/stripe/webhook`.

## Twilio (email and text messages)

Every email and text message Cairn sends goes through Twilio. The code is [`api/cairn_api/twilio_client.py`](api/cairn_api/twilio_client.py). It calls Twilio's REST APIs with `httpx`, so there's no Twilio SDK to install.

| What | Twilio product | Credentials | Status |
|---|---|---|---|
| Confirmations of things the user did (UC-CASE-21), the trial-ending note (D-14), the reminders a user chose (UC-REG-15), the opted-in check-in, the break-ending notice (UC-BRK-09), and the yearly subscription reminder (UC-SUB-17) | Twilio SendGrid, v3 Mail Send | `TWILIO_SENDGRID_API_KEY` | Live. The `outbound` job, every 5 minutes |
| The sign-in magic link (UC-REG-04) | Twilio SendGrid, through Auth0's email provider | The same SendGrid key, entered in Auth0 | Tenant setting, see [auth0/README.md](auth0/README.md) |
| Text messages | Twilio Programmable Messaging | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | Built and tested. Not in the MVP (OPEN-05, decided 2026-10-05). Turning it on later needs legal review of the consent wording |
| Verifying a phone number by code | Twilio Verify | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_VERIFY_SERVICE_SID` | Built and tested. For after the MVP, if text messages are added |

**Email needs its own key.** Twilio SendGrid doesn't accept the Account SID and Auth Token. In the SendGrid console (or Twilio console > Email), create an API key with **Mail Send** access only, and authenticate Cairn's sending domain (SPF and DKIM). Register the same domain with Apple's Private Email Relay Service so `@privaterelay.appleid.com` addresses receive mail (UC-REG-03). Open and click tracking are switched off on every message in code, so SendGrid never adds a tracking pixel or rewrites links.

**The trial.** A Twilio trial can only text verified caller IDs, adds "Sent from your Twilio trial account" to each text, and has a limited number of messages. That's why the live checks run only by hand.

### Settings

| Name | Where it's used | Secret |
|---|---|---|
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Text messages, Verify, and the credential check | Yes |
| `TWILIO_FROM_NUMBER` or `TWILIO_MESSAGING_SERVICE_SID` | The number (E.164) or Messaging Service texts come from | No |
| `TWILIO_VERIFY_SERVICE_SID` | The Verify service (Twilio console > Verify > Services) | No |
| `TWILIO_SENDGRID_API_KEY` | Email | Yes |
| `CAIRN_EMAIL_FROM` | The sender, for example `Cairn <no-reply@mail.example>` | No |
| `CAIRN_EMAIL_PROVIDER` | `twilio` (default) or `smtp` for a local mail catcher | No |

Locally, put them in `.env` (gitignored). On Cloudflare, use `wrangler secret put` ([step 6](#6-set-the-secrets)). Only the jobs container receives them, never the API container.

### GitHub secrets

The [Twilio integration](.github/workflows/twilio-integration.yml) workflow reads these as GitHub Actions secrets. Repository secrets (**Settings > Secrets and variables > Actions**) and `cairnguide` organization secrets shared with this repository both work. `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_SENDGRID_API_KEY` are set as organization secrets.

| Secret | Needed for |
|---|---|
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Every run |
| `TWILIO_FROM_NUMBER` (or `TWILIO_MESSAGING_SERVICE_SID`) and `TWILIO_TEST_TO_NUMBER` | Send one text message |
| `TWILIO_VERIFY_SERVICE_SID` and `TWILIO_TEST_TO_NUMBER` | Send one Verify code |
| `TWILIO_SENDGRID_API_KEY`, `CAIRN_EMAIL_FROM`, `TWILIO_TEST_TO_EMAIL` | Send one email |

`TWILIO_TEST_TO_NUMBER` must be a verified caller ID on a trial account. Set `TWILIO_TEST_TO_EMAIL` to an Apple relay address now and then to check UC-REG-03 delivery.

### Running the live checks

The live checks never run on a push, a pull request, or a schedule. Every push and pull request runs the mocked tests in [`api/tests/test_twilio_client.py`](api/tests/test_twilio_client.py) instead, which send nothing.

Open **Actions > Twilio integration > Run workflow**. With every box unticked, it only checks the credentials, which is free. Each ticked box sends one message, so one run uses at most two texts and one email. Or from the command line:

```bash
gh workflow run twilio-integration.yml -f send_email=true
```

To run them locally with the values in your shell:

```bash
cd api && CAIRN_TWILIO_LIVE_EMAIL=1 pytest integration -ra -s
```

## Command reference

Run `make help` for this list. Everything the Makefile does is also written out above.

| Command | What it does |
|---|---|
| `make setup` | One-time setup: `.venv`, MongoDB replica set, schema, login users, templates, test logins, `.env` |
| `make seed-test-db` | Add the test logins. `ARGS=--reset` puts the test accounts back to their starting state |
| `make dev-token` | Print a token for a test login while `make run` is up. `LOGIN=new.user` for the one with no account |
| `make run` | The API with reload on port 8000 |
| `make run-jobs` | The jobs service on port 8001 (localhost only) |
| `make job NAME=…` | Trigger one job on the local jobs service |
| `make test` | Fast tests, no database |
| `make test-db` | Every API test on a scratch database |
| `make db-check` | Data security suite on a scratch database |
| `make db-apply` | Apply `database/db/schema.py` to the local database |
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

**The API won't start: "Set MONGODB_URI in the environment."** A required setting is missing. Locally, check `.env`. On Cloudflare, check `vars` in `wrangler.jsonc` and `npx wrangler secret list`.

**Every request returns 403 `case_access_denied`, or the logs show `code=13`.** MongoDB refused the user (Unauthorized). `MONGODB_URI` must be the `cairn_api` user, holding the `cairnApp` role on the database named in `CAIRN_MONGODB_DB`. On Atlas, check the role matches `python3 database/db/apply.py --print-roles`.

**"Transaction numbers are only allowed on a replica set member or mongos".** The server is a standalone `mongod`. Start it with `--replSet rs0` and run `python3 scripts/init_replica_set.py '<admin uri>'` once (`make setup` does this).

**A request returns 409 `try_again`.** Another request changed the same document at the same moment, and MongoDB aborted this one instead of making it wait. Sending it again is safe.

**Connections time out from Cloudflare.** The Atlas IP access list doesn't admit the containers (see step 1).

**`wrangler deploy` fails to build the image.** Docker has to be running. On Apple silicon, the build emulates `linux/amd64`, which is slower but works.

**The first request is slow.** A container was starting. Raise `sleepAfter` in `cloudflare/src/index.ts` to keep containers warm longer.

**`POST /v1/dev/token` returns 404.** `CAIRN_DEV_AUTH_SECRET` isn't set. Run `make setup` again, or add a random value of at least 32 characters to `.env`, then restart `make run`. A 503 means the test logins are missing: run `make seed-test-db`.

**A browser client gets a CORS error.** Add its origin to `CAIRN_CORS_ORIGINS` (in `.env` locally, in `vars` on Cloudflare).

**Every signed-in request returns 401.** The token's audience or issuer doesn't match `CAIRN_AUTH0_AUDIENCE` and `CAIRN_AUTH0_DOMAIN`, or it's a machine-to-machine token. See [Try signed-in endpoints](#4-try-signed-in-endpoints).
