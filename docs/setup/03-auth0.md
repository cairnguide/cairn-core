# 3. Sign-in (Auth0, Google, Apple, email)

Produces: an Auth0 tenant that signs people in with Google, Apple, or an email magic link, adds Cairn's claims to the access token, and lets the jobs delete users after account deletion.

Reference, with every setting and the client flow: [auth0/README.md](../../auth0/README.md). This page puts it in setup order and adds the outside accounts it depends on.

Cairn never sees a password or a passkey. The API only verifies Auth0's signed access tokens.

## 3.1 Create the tenant

One tenant per environment. Use the region closest to the data. Note the tenant domain (for example `cairn-prod.us.auth0.com`) or set up a custom domain. This is `CAIRN_AUTH0_DOMAIN`.

## 3.2 Create the API

**Applications > APIs > Create API.** Identifier: your audience, for example `https://api.cairn.example`. Signing algorithm: **RS256**. The identifier is `CAIRN_AUTH0_AUDIENCE`. The API only accepts RS256 and fetches the keys from `https://<domain>/.well-known/jwks.json`.

## 3.3 Google

1. In Google Cloud, create a project, configure the OAuth consent screen, and create an **OAuth client ID** (web application). Add Auth0's callback, `https://<auth0-domain>/login/callback`, as an authorized redirect URI.
2. In Auth0, **Authentication > Social > google-oauth2**: paste Cairn's own client ID and secret, never Auth0's development keys.
3. Request only `openid` and `email`. Never `profile` (D-16). Cairn asks people what to call them.

## 3.4 Apple

In the Apple Developer account:

1. Create an **App ID** with Sign in with Apple, and a **Services ID** for the web flow, with Auth0's callback as the return URL. The Services ID becomes `CAIRN_APPLE_CLIENT_ID`.
2. Create a **Sign in with Apple key** (a `.p8` file). Note its Key ID (`CAIRN_APPLE_KEY_ID`) and your Team ID (`CAIRN_APPLE_TEAM_ID`). Store the `.p8` file in your secret manager. It's `CAIRN_APPLE_PRIVATE_KEY`, and you can download it only once.
3. **Private Email Relay:** under Sign in with Apple for Email Communication, register Cairn's sending domain and addresses (the ones from [5. Twilio](05-twilio.md)), so `@privaterelay.appleid.com` addresses receive Cairn's email.

In Auth0, **Authentication > Social > apple**: enter the Services ID, Team ID, Key ID, and private key. Scopes `openid` and `email` only.

## 3.5 Email (passwordless magic link)

**Authentication > Passwordless > Email**, connection name `email` (`CAIRN_AUTH0_EMAIL_CONNECTION`):

- **Link** mode, OTP expiry **900 seconds** (15 minutes, UC-REG-04).
- Sent from Cairn's own domain, through the email provider in 3.7.

The link must open a Cairn landing page that uses the token only when the person selects Continue, so an email security scanner can't use it up. Details in [auth0/README.md](../../auth0/README.md#tenant-configuration).

If you also enable a password database connection, follow NIST SP 800-63B-4 as described there (minimum 15 characters, no composition rules, Breached Password Detection on).

## 3.6 The post-login Action

**Actions > Library > Build Custom > Login / Post Login.** Paste [`auth0/actions/cairn-claims.js`](../../auth0/actions/cairn-claims.js). Add the Action secret `CLAIM_NAMESPACE` with the same value you'll set as `CAIRN_CLAIM_NAMESPACE` (for example `https://cairn.example/`). Deploy it and add it to the Login flow.

It refuses any connection other than the three above and adds `email`, `email_verified`, and `sign_in_method` to the access token.

## 3.7 Email provider

**Branding > Email Provider > SendGrid.** Paste the Twilio SendGrid API key from [5. Twilio](05-twilio.md) and set the From address to the authenticated sending domain. Then Auth0's magic links go through Twilio like every other Cairn email. Do this after step 5. Keep Auth0's passwordless SMS connection off.

## 3.8 The application

Create a **Single Page** or **Native** application for the client, with only the three connections enabled. It requests the Cairn audience. After [7. Cloudflare](07-cloudflare.md), add the app's URLs to **Allowed Callback URLs**, **Allowed Logout URLs**, and **Allowed Web Origins**.

## 3.9 Sessions

Cairn signs a session out after 5 minutes without activity (D-20), on the server. Match it in **Settings > Advanced > Login Session Management**: inactivity timeout 5 minutes, require login after 30 days. For refresh tokens: absolute lifetime 30 days, inactivity lifetime 5 minutes. Keep access tokens short, for example 10 minutes.

## 3.10 The Management API client (for account deletion)

**Applications > Machine to Machine**, authorized for the **Auth0 Management API** with exactly `read:users`, `read:user_idp_tokens`, and `delete:users`. Its client ID and secret are `CAIRN_AUTH0_MGMT_CLIENT_ID` and `CAIRN_AUTH0_MGMT_CLIENT_SECRET`. Only the jobs container receives them. The `identity_cleanup` job uses them to delete the Auth0 user and to read the Apple refresh token it revokes with Apple.

**[VERIFY]** Confirm on the real tenant that the Apple identity exposes `refresh_token` (`api/cairn_api/identity_cleanup.py`).

## What you have now

| Value | Kind | Goes to |
|---|---|---|
| `CAIRN_AUTH0_DOMAIN`, `CAIRN_AUTH0_AUDIENCE`, `CAIRN_CLAIM_NAMESPACE`, `CAIRN_AUTH0_EMAIL_CONNECTION` | Not secret | `vars` in `wrangler.jsonc` |
| `CAIRN_AUTH0_MGMT_CLIENT_ID`, `CAIRN_AUTH0_MGMT_CLIENT_SECRET` | Secret | `wrangler secret put` |
| `CAIRN_APPLE_CLIENT_ID`, `CAIRN_APPLE_TEAM_ID`, `CAIRN_APPLE_KEY_ID`, `CAIRN_APPLE_PRIVATE_KEY` | Secret (the IDs are kept with the key for simplicity) | `wrangler secret put` |
| Google client secret, Apple `.p8` key | Secret | Auth0's connection settings |

## Check it

Get a token for a real user and call `POST /v1/registrations` once the API is deployed:

```bash
auth0 test token --audience https://api.cairn.example <application-client-id>
```

A machine-to-machine token from Auth0's Test tab won't work: it has no user and no sign-in method, so the API refuses it.

Next: [4. Stripe](04-stripe.md).
