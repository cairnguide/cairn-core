# 8. GitHub (CI, deploy workflows, branch protection)

Produces: the `cloudflare` deploy environment with its secrets, the branch protection ruleset, and one-click deploys.

Reference: [.github/README.md](../../.github/README.md).

## What runs when

| Event | Workflow | What it does |
|---|---|---|
| Push to a branch other than `main` | `branch-push.yml` | Lint, plus the test files the push changed |
| Pull request into `main`, and merge to `main` | `pull-request.yml` | Lint, every test on MongoDB 7.0 and 8.0 (including the data security suite), the 90% coverage floor, and a `wrangler deploy --dry-run` |
| By hand | `deploy-cloudflare.yml` | `deploy-database.yml`, then `wrangler deploy` |
| By hand, or called by the deploy | `deploy-database.yml` | Validate templates, apply the schema, run pending migrations, load templates |
| By hand | `twilio-integration.yml` | Live Twilio checks: credentials always, one text, Verify code, or email only when ticked |

Nothing deploys automatically.

## 8.1 Create the deploy environment

**Settings > Environments > New environment**, named `cloudflare` (or one per environment, for example `staging` and `production`). Add **required reviewers** so every deploy needs an approval. Restrict it to the `main` branch under **Deployment branches**.

Name the production environment `production`. `deploy-database.yml` refuses unreviewed templates there.

## 8.2 Add its secrets and variables

| Name | Kind | What it is |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | Secret | The Workers + Containers token from [7.1](07-cloudflare.md#71-install-and-sign-in) |
| `CLOUDFLARE_ACCOUNT_ID` | Secret | The Cloudflare account |
| `CAIRN_ADMIN_MONGODB_URI` | Secret | An Atlas administrator, for `apply.py` only |
| `CAIRN_LOADER_MONGODB_URI` | Secret | The `cairn_loader` user |
| `ATLAS_PUBLIC_KEY`, `ATLAS_PRIVATE_KEY`, `ATLAS_PROJECT_ID` | Secrets, optional | An Atlas API key with Project IP Access List Admin. The workflow admits the runner's IP for the run and removes it after |
| `CAIRN_MANAGE_ROLES` | Variable | `false` on Atlas |
| `CAIRN_MONGODB_DB` | Variable, optional | `cairn` when unset |

The Worker's own secrets (Stripe, Twilio, Auth0, Apple, VAPID, the two runtime connection strings) are set with `wrangler secret put` and never stored in GitHub. The deploy doesn't touch them.

The Twilio live checks read `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_SENDGRID_API_KEY` from `cairnguide` organization secrets, plus the test recipients from repository secrets. See [.github/README.md](../../.github/README.md#twilio-integration-twilio-integrationyml).

## 8.3 Turn on branch protection

Import [`.github/rulesets/protect-main.json`](../../.github/rulesets/protect-main.json): **Settings > Rules > Rulesets > New ruleset > Import a ruleset**. It requires a pull request and the **All checks passed** job, requires branches to be up to date, and blocks force pushes and deleting `main`. It's enforced on private repositories only on a paid GitHub plan.

The approval count is 0 so a solo engineer isn't blocked. Raise `required_approving_review_count` once there's a team.

## 8.4 Dependency updates

[`.github/dependabot.yml`](../../.github/dependabot.yml) opens a weekly pull request for each part of the repository: `pip` in `/api` and `/database/tools`, `npm` in `/cloudflare`, the `Dockerfile` base image, the Codespaces `docker-compose.yml`, and the GitHub Actions the workflows use. Minor and patch updates are grouped into one pull request per ecosystem. Python stays on 3.12 and MongoDB on the versions CI tests, so Dependabot proposes only patch updates for those images. Move them on purpose.

Version updates run on a schedule. For security fixes as soon as an advisory lands, also turn on **Settings > Code security > Dependabot alerts** and **Dependabot security updates**, and **Secret scanning with push protection**.

## Deploy

```bash
gh workflow run deploy-cloudflare.yml --ref main
```

Or **Actions > Deploy to Cloudflare > Run workflow**. Tick **Load templates that haven't had counsel review** for staging only.

Next: [9. Launch checklist](09-launch-checklist.md).
