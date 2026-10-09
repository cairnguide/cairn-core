# 1. Accounts and tools

## Accounts

Create these accounts, preferably owned by a shared organization account rather than one person, with multi-factor authentication on every one of them.

| Service | Plan needed | Used for | Setup step |
|---|---|---|---|
| [Cloudflare](https://dash.cloudflare.com/) | **Workers Paid**. Containers aren't on the free plan | Hosting the Worker, both containers, and the cron triggers | [7](07-cloudflare.md) |
| [MongoDB Atlas](https://cloud.mongodb.com/) | A dedicated **M10 or larger** cluster | The database | [2](02-database.md) |
| [Auth0](https://auth0.com/) | A tenant per environment | Sign-in with Google, Apple, or email | [3](03-auth0.md) |
| [Google Cloud](https://console.cloud.google.com/) | Free | Cairn's own OAuth client for Sign in with Google | [3](03-auth0.md) |
| [Apple Developer Program](https://developer.apple.com/programs/) | Paid membership | Sign in with Apple, the Private Email Relay, token revocation | [3](03-auth0.md) |
| [Stripe](https://dashboard.stripe.com/) | Standard, with Stripe Tax | Subscriptions | [4](04-stripe.md) |
| [Twilio](https://www.twilio.com/) with SendGrid | A paid SendGrid plan before launch | Every email | [5](05-twilio.md) |
| [GitHub](https://github.com/cairnguide) | Pro, Team, or Enterprise for a private repo, so the ruleset is enforced | Code, CI, deploys, Codespaces | [8](08-github.md) |
| A domain registrar, with DNS on Cloudflare | | The API's custom domain and the email sending domain | [5](05-twilio.md), [7](07-cloudflare.md) |

## Tools on the machine that deploys

| Tool | Version | Why |
|---|---|---|
| Python | 3.11 or newer (CI uses 3.12) | `database/db/apply.py`, the template loader, tests |
| Node | 20 or newer (the Codespace has 22) | `wrangler`, the Worker |
| Docker | Running | `wrangler deploy` builds the container image locally |
| Git, GitHub CLI (`gh`) | | Cloning, running workflows |
| `openssl` | | Making the VAPID key pair |
| [Stripe CLI](https://docs.stripe.com/stripe-cli) | Optional | Forwarding webhooks while testing |
| [Auth0 CLI](https://github.com/auth0/auth0-cli) | Optional | Getting a test token |

A GitHub Codespace on this repository has Python, Node, and Docker already. See [Local development](local-development.md).

## Get the code

```bash
gh repo clone cairnguide/cairn-core
```

```bash
cd cairn-core && make setup
```

`make setup` needs a local MongoDB (see [Local development](local-development.md)). For a deploy only, you just need the Python tools:

```bash
python3 -m venv .venv && .venv/bin/pip install -r database/tools/requirements.txt
```

Next: [2. Database](02-database.md).
