# 9. Launch checklist

Work through this after the first deploy of an environment. The first part checks that it works. The second part lists what must be settled before real users.

## It works

- [ ] `GET /healthz` answers 200 on the public URL.
- [ ] `GET /v1/welcome` shows three sign-in buttons.
- [ ] Signing in with Google, Apple, and an email link each reaches `POST /v1/registrations` and gets 201 the first time, 200 after.
- [ ] A token issued more than 5 minutes before the last activity gets 401 `session_timed_out`.
- [ ] `POST /v1/dev/token` answers **404**. If it doesn't, `CAIRN_DEV_AUTH_SECRET` reached the container. Stop and fix it.
- [ ] `npx wrangler tail` shows every job running on its cron, and none failing. `outbound` and `identity_cleanup` don't answer `"skipped"`.
- [ ] A test Checkout in Stripe test mode ends with `access: full` after the webhook arrives, and the webhook shows 2xx in the Stripe dashboard.
- [ ] Deleting a test account sends one confirmation email, and within 15 minutes the Auth0 user is gone.
- [ ] An email reaches an `@privaterelay.appleid.com` address.
- [ ] A browser notification arrives, empty, with Cairn's fixed line.
- [ ] Restore the latest Atlas backup into a scratch cluster.

## Security

- [ ] `npx wrangler secret list` shows every secret in [Secrets: inventory](../security/secrets.md#inventory) for this environment, and `wrangler.jsonc` `vars` holds no secret.
- [ ] Each Atlas user holds exactly one custom role. The API's user is `cairn_api`, never an administrator.
- [ ] Every outside account (Cloudflare, Atlas, Auth0, Stripe, Twilio, Apple, Google Cloud, GitHub) has multi-factor on and at least two owners.
- [ ] Stripe uses a restricted key. The SendGrid key has Mail Send only. The Auth0 Management client has only `read:users`, `read:user_idp_tokens`, and `delete:users`.
- [ ] HSTS, Always Use HTTPS, and minimum TLS 1.2 are on for the custom domain.
- [ ] The branch protection ruleset is active, and the deploy environment needs a reviewer.
- [ ] Alerts route somewhere a person sees them: job failures and `owner alert` lines.

## Open decisions before real users

Each has an owner file. Don't guess.

| Item | Where it's tracked |
|---|---|
| Counsel review of the task and journey templates (the loader's release gate) **[LEGAL REVIEW REQUIRED]** | `api/README.md` decision 8 |
| Clinical sign-off of the distress phrase list (DEC-26-06) | `api/cairn_api/safety.py` |
| The AI provider and the AI notice's provider name **[LEGAL REVIEW REQUIRED]** | [AI agents](../ai/agents.md) |
| Retention periods for audit events, `cases.purge_after`, and finished accounts with no case **[LEGAL REVIEW REQUIRED]** | `database/CLAUDE.md`, open question 4 |
| Atlas network access from Cloudflare (open access list or a fixed-egress proxy) **[DECISION NEEDED]** | `database/CLAUDE.md`, open question 12 |
| Application-layer encryption for `ssn_last4` and legal names **[DECISION NEEDED]** | `database/CLAUDE.md`, open question 5, and [Encryption](../security/encryption.md#gaps) |
| Whether Swagger UI and `/openapi.json` stay public in production **[DECISION NEEDED]** | [Security model](../security/overview.md#open-items) |
| Confirm the Auth0 Apple identity exposes `refresh_token` | `api/cairn_api/identity_cleanup.py` |
| Security review of the sign-in email recovery process (UC-REG-20) | `database/docs/support-sign-in-email-recovery.md` |
| Stripe: subscription consent records after deletion, arbitration in the Terms **[LEGAL REVIEW REQUIRED]** | `database/CLAUDE.md`, open question 9 |
