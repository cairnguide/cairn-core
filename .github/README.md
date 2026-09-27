# CI and branch management

## What runs when

| Event | Workflow | What it checks |
|---|---|---|
| Push to any branch except `main` | [`branch-push.yml`](workflows/branch-push.yml) | Lint, plus the test files the push added or changed. If no test files changed, it runs the fast suite, which needs no database. |
| Pull request into `main` | [`pull-request.yml`](workflows/pull-request.yml) | Lint, every API test on Postgres 15 and 16 (a skipped test counts as a failure), and the database security suite (`database/db/tests/run.sh`) on Postgres 15 and 16. |
| Merge to `main` | [`pull-request.yml`](workflows/pull-request.yml) | The same full set, so `main` is re-verified after every merge. |
| Run by hand | [`deploy-cloudflare.yml`](workflows/deploy-cloudflare.yml) | Deploys to Cloudflare: migrations, templates, then the Worker and container image. See the repository README. |

"New tests" means test files under `api/tests/` that were added or modified since the previous push. On a branch's first push, or after a force push, it means every test file changed since the branch left `main`.

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
ruff check . && shellcheck database/db/*.sh database/db/tests/*.sh .github/scripts/*.sh scripts/*.sh && actionlint
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
