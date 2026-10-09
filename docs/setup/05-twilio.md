# 5. Email and text messages (Twilio)

Produces: a Twilio SendGrid API key, an authenticated sending domain, and the sender address. Every email Cairn sends, including Auth0's sign-in link, goes through it.

Reference: the root [README, "Twilio"](../../README.md#twilio-email-and-text-messages). Code: [`twilio_client.py`](../../api/cairn_api/twilio_client.py) (REST over `httpx`, no SDK) and [`outbound.py`](../../api/cairn_api/outbound.py).

## What goes out

| Message | Product | Status |
|---|---|---|
| Confirmations of things the person did, the trial-ending note, chosen reminders, the opted-in check-in, the break-ending notice, the yearly renewal reminder | Twilio SendGrid Mail Send | Live. The `outbound` job, every 5 minutes |
| The sign-in magic link | SendGrid, sent by Auth0 | Auth0 tenant setting ([3.7](03-auth0.md#37-email-provider)) |
| Text messages | Twilio Programmable Messaging | Built and tested, not offered. Not in the MVP (OPEN-05) |
| Phone number verification | Twilio Verify | Built and tested, for after the MVP |

Every message is short and private. It never names the person who died, the circumstance, task details, or anything deleted, and `outbound.py` checks that before sending. Open and click tracking are switched off on every message in code, so SendGrid never adds a tracking pixel or rewrites links.

## 5.1 Authenticate the sending domain

1. Pick a sending subdomain, for example `mail.cairn.example`.
2. In SendGrid (or Twilio console > Email), **Settings > Sender Authentication > Authenticate Your Domain**. Add the CNAME records it gives you in Cloudflare DNS. This sets up SPF and DKIM.
3. Add a DMARC record for the domain (start with `p=none` and a reporting address, then tighten).
4. Register the same domain and sender with Apple's Private Email Relay ([3.4](03-auth0.md#34-apple)).

## 5.2 Create the API key

**Settings > API Keys > Create API Key**, **Restricted Access**, with **Mail Send** only. SendGrid doesn't accept the Twilio Account SID and Auth Token. It's a separate key.

| Value | Kind | Goes to |
|---|---|---|
| `TWILIO_SENDGRID_API_KEY` (`SG....`) | Secret | `wrangler secret put` (jobs container only), and Auth0's email provider |
| `CAIRN_EMAIL_FROM`, for example `Cairn <no-reply@mail.cairn.example>` | Not secret, but set as a secret today | `wrangler secret put` |
| `CAIRN_EMAIL_PROVIDER` | `twilio` | `vars` in `wrangler.jsonc` |

Until the key is set, the `outbound` job answers `"skipped"` and confirmations wait in the queue.

## 5.3 Text messages and Verify (not in the MVP)

Nothing sends a text to users. If text messages are added later, it needs legal review of the consent wording **[LEGAL REVIEW REQUIRED]** and an encrypted field for the phone number with its own key management. A phone number is never stored in plain text. The credentials, when needed:

| Value | Kind |
|---|---|
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Secret |
| `TWILIO_FROM_NUMBER` or `TWILIO_MESSAGING_SERVICE_SID` | Not secret |
| `TWILIO_VERIFY_SERVICE_SID` | Not secret |

## 5.4 Leave the trial before launch

A Twilio or SendGrid trial limits volume, and a Twilio trial adds a banner to texts and only texts verified numbers. Move to a paid plan before real users.

## Check it

**Actions > Twilio integration > Run workflow** with every box unticked checks the credentials for free. Tick **send email** to send one email to `TWILIO_TEST_TO_EMAIL`. Send one to an Apple relay address now and then to check relay delivery. See [8. GitHub](08-github.md).

Next: [6. Browser notifications](06-browser-notifications.md).
