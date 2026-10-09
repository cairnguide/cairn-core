# Third-party integrations

Every outside service Cairn depends on: what it's used for, what data it receives, which credential it uses, and where the code is. If you add a service, add it here, with what it receives.

## At a glance

| Service | Used for | Status | Receives personal data | Setup |
|---|---|---|---|---|
| [Cloudflare Workers and Containers](#cloudflare) | Hosting the API, the jobs, and the cron triggers | Live | Yes, in transit (every request) | [7](../setup/07-cloudflare.md) |
| [MongoDB Atlas](#mongodb-atlas) | The database | Live | Yes, all of it | [2](../setup/02-database.md) |
| [Auth0](#auth0) | Sign-in, sessions, identity deletion | Live | Email address, sign-in identities | [3](../setup/03-auth0.md) |
| [Google](#google-sign-in) | Sign in with Google | Live | Through Auth0: email | [3.3](../setup/03-auth0.md#33-google) |
| [Apple](#apple) | Sign in with Apple, Private Email Relay, token revocation | Live | Through Auth0: email or relay address | [3.4](../setup/03-auth0.md#34-apple) |
| [Stripe](#stripe) | Subscriptions, payment, tax, invoices | Live | Account id, sign-in email, everything entered on Stripe's pages | [4](../setup/04-stripe.md) |
| [Twilio SendGrid](#twilio-sendgrid) | Every email, including Auth0's sign-in link | Live | Email address, short private message | [5](../setup/05-twilio.md) |
| [Twilio Messaging and Verify](#twilio-programmable-messaging-and-verify) | Text messages, phone verification | Built, not in the MVP | Would receive a phone number | [5.3](../setup/05-twilio.md#53-text-messages-and-verify-not-in-the-mvp) |
| [Browser push services](#browser-push-services) | Browser notifications | Live when the VAPID key is set | An opaque endpoint. No content | [6](../setup/06-browser-notifications.md) |
| [GitHub](#github) | Code, CI, deploys, Codespaces, secrets | Live | No user data | [8](../setup/08-github.md) |
| [Container and package registries](#container-and-package-registries) | Base images and dependencies | Build time | No | |
| [AI model provider](#ai-model-provider-planned) | The conversation, reading free text, crisis detection | **[NOT BUILT]** | Would receive redacted text and case context | [AI agents](../ai/agents.md) |
| [Official sources](#official-sources) | Citations in task templates | Links only | No | |
| [Patreon](#patreon) | The repository's sponsor link | Links only | No | |

## Cloudflare

| | |
|---|---|
| **Products** | Workers, Containers, Durable Objects (backing the containers), Cron Triggers, Workers Logs, the container image registry, DNS and certificates for the custom domain |
| **Used for** | The `cairn-api` Worker is the public front door. It forwards every request to the `CairnApi` container and runs the scheduled jobs in the `CairnJobs` container |
| **Data** | Every request and response passes through it. Logs hold request metadata and job counts. No user data is stored there |
| **Credentials** | `wrangler login`, or `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` in CI. Holds every runtime secret |
| **Code** | `cloudflare/src/index.ts`, `cloudflare/wrangler.jsonc`, `Dockerfile` |
| **Plan** | Workers Paid (Containers aren't on the free plan) |

## MongoDB Atlas

| | |
|---|---|
| **Products** | A dedicated cluster (M10+), Cloud Backup, custom database roles, the IP access list, and the Atlas Admin API (for the deploy workflow's access list entry) |
| **Used for** | All user, case, template, and operational data. Every request is one multi-document transaction |
| **Data** | Everything described in [Data model](../architecture/data-model.md) |
| **Credentials** | `MONGODB_URI` (API), `CAIRN_JOBS_MONGODB_URI` (jobs), `CAIRN_LOADER_MONGODB_URI` and `CAIRN_ADMIN_MONGODB_URI` (deploy), optional `ATLAS_*` API key (deploy) |
| **Code** | `api/cairn_api/db.py`, `store.py`, `maintenance.py`, `database/` |

## Auth0

| | |
|---|---|
| **Products** | Universal Login, social connections (Google, Apple), Passwordless email, a post-login Action, the Management API, the email provider setting |
| **Used for** | Signing people in with three methods, issuing RS256 access tokens, sending the magic link (through SendGrid), and deleting the Auth0 user after account deletion |
| **Data** | Email address, sign-in identities, Google and Apple tokens |
| **Credentials** | The API needs only the domain and audience (it fetches public keys). The jobs use `CAIRN_AUTH0_MGMT_CLIENT_ID` and `_SECRET` |
| **Code** | `api/cairn_api/auth.py`, `identity_cleanup.py`, `auth0/actions/cairn-claims.js` |

## Google (sign-in)

| | |
|---|---|
| **Used for** | Sign in with Google, through Auth0's google-oauth2 connection with Cairn's own OAuth client |
| **Data** | `openid` and `email` scopes only. Never `profile` |
| **Credentials** | Google OAuth client ID and secret, held in Auth0 |

## Apple

| | |
|---|---|
| **Products** | Sign in with Apple, Private Email Relay Service, the token revocation endpoint |
| **Used for** | Signing in. Delivering email to `@privaterelay.appleid.com` addresses. Revoking the Apple token after account deletion (an App Store requirement) |
| **Data** | Email or relay address. On deletion, the refresh token is sent to `appleid.apple.com/auth/revoke` |
| **Credentials** | Services ID, Team ID, Key ID, and `.p8` key: in Auth0's apple connection, and `CAIRN_APPLE_*` for the jobs |
| **Code** | `api/cairn_api/identity_cleanup.py` |

## Stripe

| | |
|---|---|
| **Products** | Checkout (hosted payment page), Billing (Subscriptions, Invoices), the customer portal, Stripe Tax, webhooks |
| **Used for** | The $14.99 monthly subscription after 28 free days. Payment, tax, invoices, failed-payment emails, refunds, and disputes |
| **Data** | Cairn sends the account id and sign-in email only. Card, bank, and billing address are entered on Stripe's pages and never reach Cairn |
| **Credentials** | `CAIRN_STRIPE_SECRET_KEY` (API and jobs), `CAIRN_STRIPE_WEBHOOK_SECRET` (API) |
| **Code** | `api/cairn_api/stripe_client.py` (REST over `httpx`, no SDK), `subscription.py`, `routers/subscription.py` |
| **Incoming** | `POST /v1/stripe/webhook`, verified with HMAC-SHA256 |

## Twilio SendGrid

| | |
|---|---|
| **Products** | v3 Mail Send API, sender authentication (SPF, DKIM) |
| **Used for** | Every email: confirmations, the trial-ending note, chosen reminders, the opted-in check-in, the break-ending notice, the yearly renewal reminder. Auth0 also sends the magic link through it |
| **Data** | The recipient address and a short, private message. Open and click tracking are turned off on every message |
| **Credentials** | `TWILIO_SENDGRID_API_KEY` (Mail Send only), `CAIRN_EMAIL_FROM` |
| **Code** | `api/cairn_api/twilio_client.py` (`SendGridMailer`), `outbound.py` |

## Twilio Programmable Messaging and Verify

| | |
|---|---|
| **Status** | Built and tested, not offered. Text messages aren't in the MVP (OPEN-05, 2026-10-05) |
| **Would be used for** | Text reminders, and verifying a phone number by code before using it |
| **Before turning on** | Legal review of the consent wording **[LEGAL REVIEW REQUIRED]**, an encrypted field for the number, and its key management |
| **Credentials** | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` or `TWILIO_MESSAGING_SERVICE_SID`, `TWILIO_VERIFY_SERVICE_SID` |
| **Code** | `api/cairn_api/twilio_client.py` (`TwilioClient`), live checks in `api/integration/` |

## Browser push services

| | |
|---|---|
| **Products** | Whichever push service the person's browser uses (Google, Mozilla, Apple, or Microsoft), through the Web Push protocol |
| **Used for** | "A notification in this browser" reminders |
| **Data** | An empty push to an opaque endpoint. No text, no ids |
| **Credentials** | `CAIRN_VAPID_PRIVATE_KEY` (jobs), `CAIRN_VAPID_PUBLIC_KEY` (API, given to the browser), `CAIRN_VAPID_SUBJECT` |
| **Code** | `api/cairn_api/webpush.py` |

## GitHub

| | |
|---|---|
| **Products** | Repository `cairnguide/cairn-core`, Actions, environments and environment secrets, organization secrets, rulesets, Codespaces, Dependabot |
| **Used for** | Code review, CI on every push and pull request, hand-run deploys, the Twilio live checks, development environments |
| **Data** | No user data. Tests use fake data only (`example.test`) |
| **Credentials** | Deploy secrets in the `cloudflare` environment. Twilio test secrets at the organization |
| **Code** | `.github/` |

## Container and package registries

| Source | What | Where |
|---|---|---|
| Docker Hub | `python:3.12-slim` (the runtime image), `mongo:8.0` and `mongo:7.0` (CI and local) | `Dockerfile`, `.devcontainer/docker-compose.yml`, `.github/scripts/start-mongodb.sh` |
| Microsoft Container Registry | `mcr.microsoft.com/devcontainers/python:1-3.12-bookworm` | `.devcontainer/docker-compose.yml` |
| PyPI | `fastapi`, `uvicorn`, `pydantic`, `pymongo`, `pyjwt[crypto]`, `httpx`, `pyyaml`, plus test and lint tools | `api/pyproject.toml`, `database/tools/requirements.txt` |
| npm | `@cloudflare/containers`, `wrangler`, `typescript` | `cloudflare/package.json`, `cloudflare/package-lock.json` |

The API talks to every provider with plain `httpx` instead of vendor SDKs, which keeps the dependency list short. Base images are pinned by tag, not digest. Dependabot proposes updates to all of these weekly ([8.4](../setup/08-github.md#84-dependency-updates)).

## AI model provider (planned)

**[NOT BUILT]**. No model is called today. Free-text reading, crisis detection, and account chat are rule-based stand-ins. The plan is for Cloudflare's Agents SDK and AI Gateway, routing to a model provider. The choice of provider is open, and `CAIRN_AI_PROVIDER_NAME` must name it on the privacy step **[LEGAL REVIEW REQUIRED]**. See [AI agents on Cloudflare](../ai/agents.md).

## Official sources

Task and journey templates cite official instructions (for example SSA, IRS, VA, state vital records offices) by `https://` URL (`journeys/sources/sources.json`, `citations` on each template). These are links shown to people. Cairn doesn't call them.

The crisis resources (988, the Veterans Crisis Line, 911) are phone numbers and links shown to people, not integrations.

## Patreon

`.github/FUNDING.yml` shows a sponsor link to "Cairn Guide" on Patreon on the GitHub repository page. Nothing in the app uses it.
