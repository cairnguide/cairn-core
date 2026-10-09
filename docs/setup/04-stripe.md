# 4. Payments (Stripe)

Produces: the Cairn subscription in Stripe, Stripe Tax, the customer portal, an API key, and (after the first deploy) the webhook.

Reference: the root [README, "Stripe"](../../README.md#stripe-subscriptions), and the spec [`database/docs/cairn-subscription-use-cases-v33.json`](../../database/docs/cairn-subscription-use-cases-v33.json). Code: [`stripe_client.py`](../../api/cairn_api/stripe_client.py) (Stripe's REST API over `httpx`, no Stripe SDK), [`subscription.py`](../../api/cairn_api/subscription.py), and [`routers/subscription.py`](../../api/cairn_api/routers/subscription.py).

## How purchase processing works

Cairn is free for 28 days from the first journey, then $14.99 a month for the whole account (D-04).

- **Checkout.** `POST /v1/me/subscription/checkout` creates a Stripe Checkout Session and returns its URL. The person pays on Stripe's own page. Cairn sends Stripe only the account id and the sign-in email (SUB-D-10), and never receives a card, bank account, or billing address (SUB-D-01). The price comes from `CAIRN_SUBSCRIPTION_PRICE_CENTS` on every session, tax inclusive, with automatic tax (SUB-D-12). The API refuses to start if any price in the copy doesn't match it.
- **Access follows Stripe's events, never the browser.** Coming back from Checkout grants nothing. `POST /v1/stripe/webhook` verifies each event's signature and timestamp, records its id once (ids, type, and times, never the payload), reads the subscription's current state from Stripe, and applies it to `users.access` and `users.subscription_status`.
- **Payment details and invoices** are on Stripe's customer portal (`POST /v1/me/subscription/portal`).
- **Cancel and undo** go through the Subscriptions API (`/cancel`, `/undo-cancel`). Account deletion cancels the subscription first.
- **Retries.** The jobs finish what couldn't be finished at the time: `process_stripe_events` and `retry_cancellations` every 15 minutes, and `sync_early_subscriptions` hourly (an early subscriber's first charge stays at the end of the free days, UC-SUB-06).
- **Failed payments.** Stripe sends its own emails. Cairn sends none (SUB-D-08).

See the [purchase sequence diagram](../architecture/overview.md#buying-a-subscription).

## 4.1 Configure the Stripe account

Do this in test mode first (a sandbox), then repeat in live mode for production.

1. **Product.** Create the Cairn subscription product. Choose its tax code with an accountant. Its id (`prod_...`) is `CAIRN_STRIPE_PRODUCT_ID`. You don't need a Price object: the API sends the price with each session.
2. **Stripe Tax.** Turn it on and register in the places you need to collect tax.
3. **Customer portal.** Settings > Billing > Customer portal: allow updating payment methods and viewing invoices. No retention offers or coupons (SUB-D-04).
4. **Emails.** Turn on Stripe's failed-payment and receipt emails (SUB-D-08).
5. **Branding.** Set the business name, icon, and colours that Checkout and the portal show.

## 4.2 Create an API key

Use a **restricted key** with only what `stripe_client.py` calls: Checkout Sessions (write: create and expire), Customer portal (write: create a session), Subscriptions (write: read, update, cancel), Invoices (read: the subscription's latest invoice), and Events (read). If Stripe refuses a call in test mode, widen only that permission. This is `CAIRN_STRIPE_SECRET_KEY` (`rk_live_...` or `sk_live_...` in production, `..._test_...` in test mode). Both the API and the jobs containers receive it.

## 4.3 Set the non-secret settings

In `vars` in `cloudflare/wrangler.jsonc` ([7. Cloudflare](07-cloudflare.md)):

| Variable | Value |
|---|---|
| `CAIRN_SUBSCRIPTION_PRICE_CENTS` | `1499` |
| `CAIRN_STRIPE_PRODUCT_ID` | `prod_...` |
| `CAIRN_APP_URL` | The web app's address. Stripe sends people back here after Checkout and the portal |
| `CAIRN_PRICE_CHANGE_EFFECTIVE_DATE`, `CAIRN_PRICE_CHANGE_NEW_PRICE` | Only while a price change is scheduled (UC-SUB-18) |

## 4.4 Create the webhook (after the first deploy)

Once [7. Cloudflare](07-cloudflare.md) gives you the API's URL, add a webhook endpoint at `https://<api host>/v1/stripe/webhook` for exactly these events:

- `checkout.session.completed`
- `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`
- `invoice.paid`, `invoice.payment_failed`, `invoice.payment_action_required`
- `customer.updated`
- `charge.refunded`, `charge.dispute.created`

Put its signing secret (`whsec_...`) in `CAIRN_STRIPE_WEBHOOK_SECRET` with `npx wrangler secret put`. Only the API container receives it.

Until the Stripe key is set, the subscription routes answer that payments aren't available and nothing is charged.

## 4.5 Owner alerts

A dispute, a reversed charge, an event for an unknown customer, or a subscription that can't be cancelled before an account is deleted logs an `owner alert` line at ERROR from `cairn_api.subscription` or `cairn_api.account`, with ids only. Route those to whoever owns billing (a Workers Logs alert or a log drain).

## Test it

With the API running locally (`make run`) and test mode keys in `.env`:

```bash
stripe listen --forward-to localhost:8000/v1/stripe/webhook
```

Put the `whsec_...` it prints in `CAIRN_STRIPE_WEBHOOK_SECRET`, restart, and go through Checkout with a [test card](https://docs.stripe.com/testing). Use test clocks to move through the free days and renewals. The automated tests use a mocked Stripe client and fixture events signed with a test secret, so they need none of this.

Next: [5. Twilio](05-twilio.md).
