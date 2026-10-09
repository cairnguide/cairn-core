# Cairn documentation

Everything about how Cairn is built, set up, secured, and connected to other services, in one place. The folder READMEs (`api/`, `database/`, `auth0/`, `.github/`) stay as the detailed reference for their own code. These pages tie them together and link to them where the detail lives.

Cairn walks a family through the logistics of a death, one step at a time. The data is highly sensitive, so security comes first. Read [Security model](security/overview.md) before changing anything that touches user or case data.

## Where to start

| You want to | Read |
|---|---|
| Understand how the pieces fit | [Architecture overview](architecture/overview.md) |
| Stand up a new environment from nothing | [Setup guide](setup/README.md), steps 1 to 9 in order |
| Run it on your laptop or in a Codespace | [Local development](setup/local-development.md) |
| Know where a secret lives and how to rotate it | [Secrets](security/secrets.md) |
| Know what's encrypted, where, and how | [Encryption](security/encryption.md) |
| Know what a third-party service is used for | [Third-party integrations](integrations/README.md) |
| Plan the AI agents | [AI agents on Cloudflare](ai/agents.md) (design, not built yet) |

## Contents

### Architecture

- [Overview](architecture/overview.md): system diagram, runtime components, trust boundaries, and the main flows (sign-in, a signed-in request, a purchase, a scheduled job, a deploy)
- [Data model](architecture/data-model.md): the MongoDB collections, who can touch them, and how the case boundary works

### Setup, from the ground up

1. [Accounts and tools](setup/01-accounts-and-tools.md)
2. [Database (MongoDB Atlas)](setup/02-database.md)
3. [Sign-in (Auth0, Google, Apple)](setup/03-auth0.md)
4. [Payments (Stripe)](setup/04-stripe.md)
5. [Email and text messages (Twilio)](setup/05-twilio.md)
6. [Browser notifications (Web Push)](setup/06-browser-notifications.md)
7. [Cloudflare (Worker, containers, cron)](setup/07-cloudflare.md)
8. [GitHub (CI, deploy workflows, branch protection)](setup/08-github.md)
9. [Launch checklist](setup/09-launch-checklist.md)

Also: [Local development](setup/local-development.md).

### Security

- [Security model](security/overview.md): the case boundary, least-privilege roles, the API and jobs split, sign-in and sessions, and what is never stored or logged
- [Secrets](security/secrets.md): every secret, where it's stored, which process receives it, and how to rotate it
- [Encryption](security/encryption.md): in transit and at rest, hop by hop, and the open gaps

### Integrations

- [Third-party integrations](integrations/README.md): every outside service, what it's used for, what data it receives, and where the code is

### AI

- [AI agents on Cloudflare](ai/agents.md): what exists today (rule-based stand-ins and the voice prompts), and the proposed design on Cloudflare's Agents SDK and AI Gateway

## Status markers

These pages use the same markers as the specs and the code:

| Marker | Meaning |
|---|---|
| **[LEGAL REVIEW REQUIRED]** | Needs counsel before real users see it |
| **[DECISION NEEDED]** | An open product or security decision. Don't guess |
| **[NOT BUILT]** | Designed or planned, with no code yet |
| **[GAP]** | Something these docs found missing or misconfigured |

## Keeping these docs current

- When you add an environment variable, add it to [Secrets](security/secrets.md) (secret or not), `.env.example`, and `API_KEYS` or `JOBS_KEYS` in `cloudflare/src/index.ts`.
- When you add an outside service, add it to [Third-party integrations](integrations/README.md) with what data it receives.
- When you add a collection, add it to [Data model](architecture/data-model.md) and to `CASE_SCOPED` in `store.py` if it holds case data.
- The diagrams are [Mermaid](https://mermaid.js.org/), which GitHub renders. Edit them as text.
