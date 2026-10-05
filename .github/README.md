# CI and branch management

## What runs when

| Event | Workflow | What it checks |
|---|---|---|
| Push to any branch except `main` | [`branch-push.yml`](workflows/branch-push.yml) | Lint, plus the test files the push added or changed. If no test files changed, it runs the fast suite, which needs no database. |
| Pull request into `main` | [`pull-request.yml`](workflows/pull-request.yml) | Lint, then every API test, including the data security suite (`api/tests/test_data_security.py`), on MongoDB 7.0 and 8.0. A skipped test counts as a failure. |
| Merge to `main` | [`pull-request.yml`](workflows/pull-request.yml) | The same full set, so `main` is re-verified after every merge. |
| Run by hand | [`deploy-cloudflare.yml`](workflows/deploy-cloudflare.yml) | Deploys to Cloudflare: the MongoDB schema, templates, then the Worker and container image. See the repository README. |
| Run by hand | [`twilio-integration.yml`](workflows/twilio-integration.yml) | Live checks against Twilio. Always checks the credentials (free). Sends one text, one Verify code, or one email only for the boxes you tick. Never runs on a push, a pull request, or a schedule, to save the Twilio trial's messages. |

Database tests run against a throwaway MongoDB replica set with authentication on, started by [`scripts/start-mongodb.sh`](scripts/start-mongodb.sh) in Docker. The API connects to it as a `cairnApp` user, exactly as in production.

"New tests" means test files under `api/tests/` that were added or modified since the previous push. On a branch's first push, or after a force push, it means every test file changed since the branch left `main`.

Every push and pull request runs the mocked Twilio tests in `api/tests/test_twilio_client.py`, which send nothing. The live tests are in `api/integration/`, outside pytest's `testpaths`, so those workflows never collect them.

### Twilio integration ([`twilio-integration.yml`](workflows/twilio-integration.yml))

Open **Actions > Twilio integration > Run workflow**, or:

```bash
gh workflow run twilio-integration.yml -f send_sms=true
```

It reads these secrets, from the repository or from the `cairnguide` organization (the Twilio SID, Auth Token, and SendGrid key are organization secrets):

| Secret | Needed for |
|---|---|
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Every run |
| `TWILIO_FROM_NUMBER` (or `TWILIO_MESSAGING_SERVICE_SID`), `TWILIO_TEST_TO_NUMBER` | One text message |
| `TWILIO_VERIFY_SERVICE_SID`, `TWILIO_TEST_TO_NUMBER` | One Verify code |
| `TWILIO_SENDGRID_API_KEY`, `CAIRN_EMAIL_FROM`, `TWILIO_TEST_TO_EMAIL` | One email |

A box ticked with a secret missing fails the run, so green always means the message went out. Only one run happens at a time. The repository README's "Twilio" section has the details.

### Lint ([`lint.yml`](workflows/lint.yml), shared by both)

- **Python:** `ruff check .` using [`ruff.toml`](../ruff.toml)
- **Shell scripts:** `shellcheck`
- **Workflows:** `actionlint`
- **Auth0 Action:** `node --check`
- **Task templates:** JSON Schema and content rules (`load_templates.py --dry-run --allow-unreviewed`). The counsel review gate stays with release tagging.
- **API contract:** `api/openapi.json` must match the code. After changing an endpoint or schema, run `python api/scripts/export_openapi.py` and commit the result.
- **Cloudflare:** the Worker type-checks (`tsc`) and `wrangler deploy --dry-run` builds the container image and bundles the Worker without uploading anything.

Run the same checks locally before pushing:

```bash
pip install -e 'api[test,lint]'
ruff check . && shellcheck .github/scripts/*.sh scripts/*.sh && actionlint
cd api && pytest
cd ../cloudflare && npm ci && npx wrangler types && npx tsc --noEmit && npx wrangler deploy --dry-run
```

## Blocking merges until everything passes

The pull request workflow ends with one job, **All checks passed**, which fails unless every other job succeeded. The ruleset in [`rulesets/protect-main.json`](rulesets/protect-main.json) makes that job required for `main`. It also:

- requires a pull request for every change to `main` (no direct pushes)
- requires the branch to be up to date with `main` before merging
- requires review conversations to be resolved
- blocks force pushes to `main` and deleting it

The approval count is 0 so a solo engineer isn't blocked. Raise `required_approving_review_count` once there's a team.

### Turning it on

The ruleset isn't active until it's imported. Either:

- **Web:** Settings > Rules > Rulesets > New ruleset > Import a ruleset, then choose `protect-main.json`.
- **CLI:** `gh api -X POST repos/Skullie131/cairn_core/rulesets --input .github/rulesets/protect-main.json`

Rulesets and branch protection are only **enforced on private repositories on a paid GitHub plan** (Pro, Team, or Enterprise). On GitHub Free a private repository can save the ruleset, but merges won't be blocked. The checks still run and show red or green on every pull request either way.
