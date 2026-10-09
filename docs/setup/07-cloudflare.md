# 7. Cloudflare (Worker, containers, cron)

Produces: the `cairn-api` Worker, the `CairnApi` and `CairnJobs` containers, the four cron triggers, and the public URL.

Reference, with every command: the root [README, "Deploy to Cloudflare"](../../README.md#deploy-to-cloudflare). Code: [`cloudflare/src/index.ts`](../../cloudflare/src/index.ts) and [`cloudflare/wrangler.jsonc`](../../cloudflare/wrangler.jsonc).

## What gets deployed

| Piece | Configured in | Notes |
|---|---|---|
| Worker `cairn-api` | `wrangler.jsonc` `name`, `main` | The front door. Forwards every request to an API container. Runs the cron jobs |
| `CairnApi` container class | `containers[0]`, `durable_objects` | `basic` instance, up to 3. Sleeps after 15 minutes idle |
| `CairnJobs` container class | `containers[1]` | `basic` instance, 1. Sleeps after 5 minutes idle. Never routed public traffic |
| Container image | `../Dockerfile` | Built locally by wrangler for `linux/amd64` and pushed to Cloudflare's registry |
| Cron triggers | `triggers.crons` | Must match `JOBS_BY_CRON` in `src/index.ts` |
| Logs | `observability.enabled: true` | Workers Logs. Jobs log counts and status only |

## 7.1 Install and sign in

```bash
cd cloudflare && npm ci && npx wrangler types
```

```bash
npx wrangler login
```

In CI or a Codespace, set `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` instead. Create the token under **My Profile > API Tokens** from the **Edit Cloudflare Workers** template, add **Account > Containers > Edit** if it's missing, and limit it to the one account.

## 7.2 Set the non-secret settings

Edit `vars` in `cloudflare/wrangler.jsonc`. Every value there is visible to anyone who can read the repository, so nothing secret goes in it. Settings changed in the Cloudflare dashboard are overwritten by this file on the next deploy.

The full list, with what each one does, is in [Secrets: configuration](../security/secrets.md#non-secret-configuration). The ones every environment must change from the example values:

- `CAIRN_AUTH0_DOMAIN`, `CAIRN_AUTH0_AUDIENCE`, `CAIRN_CLAIM_NAMESPACE` (from [3](03-auth0.md))
- `CAIRN_TERMS_VERSION`, `CAIRN_PRIVACY_VERSION`, and the four public URLs
- `CAIRN_AI_PROVIDER_NAME` (the AI provider named on the privacy step, **[LEGAL REVIEW REQUIRED]**, see [AI agents](../ai/agents.md))
- `CAIRN_APP_URL`, `CAIRN_SUBSCRIPTION_PRICE_CENTS`, `CAIRN_STRIPE_PRODUCT_ID` (from [4](04-stripe.md))
- `CAIRN_VAPID_PUBLIC_KEY`, `CAIRN_VAPID_SUBJECT` (from [6](06-browser-notifications.md))
- `CAIRN_CORS_ORIGINS`, only for a web client on another origin

Keep `max_instances × CAIRN_DB_POOL_MAX` under the Atlas cluster's connection limit.

## 7.3 Set the secrets

Each command prompts for the value, which is encrypted and stored by Cloudflare and never shown again. Nothing goes in the repository.

```bash
npx wrangler secret put MONGODB_URI
```

Repeat for each secret in [Secrets: inventory](../security/secrets.md#inventory) whose destination is "Cloudflare secret". At minimum: `MONGODB_URI`, `CAIRN_JOBS_MONGODB_URI`, `TWILIO_SENDGRID_API_KEY`, `CAIRN_EMAIL_FROM`, `CAIRN_AUTH0_MGMT_CLIENT_ID`, `CAIRN_AUTH0_MGMT_CLIENT_SECRET`, the four `CAIRN_APPLE_*` values, `CAIRN_STRIPE_SECRET_KEY`, and `CAIRN_VAPID_PRIVATE_KEY`. For files:

```bash
npx wrangler secret put CAIRN_APPLE_PRIVATE_KEY < AuthKey_XXXXXXXXXX.p8
```

The Worker holds every secret, but each container receives only the names in its list: `API_KEYS` or `JOBS_KEYS` in `src/index.ts`. Adding a name to a list is a security decision. `CAIRN_DEV_AUTH_SECRET` must never be added to either.

`wrangler secret put` before the first deploy creates the Worker with only the secret. That's expected.

## 7.4 Deploy

Apply the schema first ([2.5](02-database.md#25-apply-the-schema-and-load-the-templates)), then:

```bash
npx wrangler deploy
```

After the first deploy, use the [GitHub workflow](08-github.md) instead, which does the database and the Worker together.

Containers take a few minutes to be ready the first time. Then:

```bash
curl https://cairn-api.<your-subdomain>.workers.dev/healthz
```

```bash
curl https://cairn-api.<your-subdomain>.workers.dev/v1/welcome
```

## 7.5 Custom domain and edge settings

1. **Workers & Pages > cairn-api > Settings > Domains & Routes > Add > Custom domain**, for example `api.cairn.example`. The zone's DNS has to be on Cloudflare. Cloudflare issues and renews the certificate.
2. On the zone, **SSL/TLS > Edge Certificates**: turn on **Always Use HTTPS**, set **Minimum TLS Version** to 1.2, and turn on **HSTS** once you're sure the domain will stay HTTPS-only. The API sets no security headers of its own **[GAP]**. See [Encryption](../security/encryption.md#gaps).
3. Consider turning off the `workers.dev` route once the custom domain works, so there's one public address.
4. Then finish the steps that needed the URL: the [Stripe webhook](04-stripe.md#44-create-the-webhook-after-the-first-deploy) and the [Auth0 allowed URLs](03-auth0.md#38-the-application).

## Operating it

| Task | Command |
|---|---|
| Stream logs, including each job's result | `npx wrangler tail` |
| List deployments | `npx wrangler deployments list` |
| Roll back the Worker and image (not the schema) | `npx wrangler rollback` |
| List secret names | `npx wrangler secret list` |
| Run the cron handler locally | `npx wrangler dev --test-scheduled`, then `curl "http://localhost:8787/__scheduled?cron=7+*+*+*+*"` |

Don't connect the repository to **Workers Builds**. It would deploy every merge without applying the schema first.

Next: [8. GitHub](08-github.md).
