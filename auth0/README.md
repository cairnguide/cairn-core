# Auth0 setup for Cairn

Cairn accounts can be created three ways: **Google**, **Apple**, or an **email address**. Email uses a passwordless magic link by default (spec decision D-10). Auth0 handles all three, including email verification and multi-factor. Cairn never sees a password.

The requirements come from `database/docs/cairn-account-use-cases-v32.json` (account spec 3.2.0: UC-REG-02 to UC-REG-05, UC-REG-18 to UC-REG-20, D-20, UC-ACCT-01).

## Tenant configuration

1. **API.** Applications > APIs > Create API. Set the identifier to your audience (for example `https://api.cairn.example`) and the signing algorithm to RS256. This value is `CAIRN_AUTH0_AUDIENCE`.
2. **Connections.** Enable exactly these for the Cairn application and disable all others:
   - Social > **google-oauth2**, using Cairn's own Google OAuth client ID and secret, not Auth0's development keys.
   - Social > **apple**, using an Apple Services ID, Team ID, Key ID, and private key from the Apple Developer account. Request only the `openid` and `email` scopes for Google and Apple, never `profile` or `name` (D-16, data_boundary.enforcement). Cairn asks the user what to call them and never pre-fills it from a provider (UC-REG-11).
   - **Apple Private Email Relay.** In the Apple Developer account, register Cairn's outbound email domain and sending addresses under Sign in with Apple for Email Communication, and authenticate the domain with SPF and DKIM. Then confirm that verification, trial reminder, and support emails reach a `@privaterelay.appleid.com` address. Cairn sends one trial-ending note per account (D-14) and a handful of confirmations, well under Apple's limit of 100 emails per day per relay address.
   - Passwordless > **Email** (connection name `email`, the default `CAIRN_AUTH0_EMAIL_CONNECTION`). Use **link** mode, set the OTP expiry to **900 seconds** (15 minutes, UC-REG-04), and send from Cairn's own email domain (see Apple relay below). Auth0 links are single use. The client starts it with `/passwordless/start` and `send: "link"`. It shows "Send a new link" when a link has expired, and offers Resend plus a spelling check after 60 seconds. The link must open a Cairn landing page (`magic_link` in `GET /v1/welcome`) that uses the token only when the user selects Continue, so an email security scanner opening the link never uses it up (UC-REG-04). If the link is opened in another browser, the user is signed in there, and the original tab offers "Send a new link here".
   - Passwords are optional. If a database connection is also enabled, follow NIST SP 800-63B-4: minimum length **15** when the password is the only factor, allow at least **64** characters (check the tenant's maximum), password policy **None** (no composition rules), **Breached Password Detection** on, and no forced periodic rotation.
3. **Email provider (Twilio SendGrid).** Branding > Email Provider > **SendGrid**. Paste the same Twilio SendGrid API key the jobs use (`TWILIO_SENDGRID_API_KEY`, README "Twilio"), and set the From address to Cairn's authenticated sending domain. Auth0 then sends the magic link (UC-REG-04) through Twilio, like every other Cairn email. Use Branding > Email Templates > Send Test Email to check it, which uses one trial email. Auth0's passwordless SMS connection, which also runs on Twilio, stays off: Cairn doesn't sign people in by text.
4. **Action.** Actions > Library > Build Custom > post-login. Paste `actions/cairn-claims.js`, add the secret `CLAIM_NAMESPACE` with the same value as `CAIRN_CLAIM_NAMESPACE`, then deploy it and add it to the Login flow.
5. **Management API client for account deletion.** Applications > Machine to Machine, authorized for the Auth0 Management API with `read:users`, `read:user_idp_tokens`, and `delete:users`. The identity cleanup worker (`api/cairn_api/identity_cleanup.py`) uses it to delete the Auth0 user and, for Apple, to read the refresh token it revokes with Apple. Keep its secret and the Apple private key in the secret manager.
6. **Application.** A single-page or native application with the three connections enabled. The client requests the Cairn audience so the access token is issued for the API.
7. **Sessions (D-20, UC-REG-19).** Cairn signs a session out after 5 minutes with no activity, on the server: a request with a token issued before a quiet stretch of more than 5 minutes gets 401 `session_timed_out`, and the client shows `session.session_timed_out` and signs in again. Match it in the tenant: Settings > Advanced > Login Session Management, **Inactivity timeout 5 minutes** and **Require log in after 30 days**, and for refresh tokens an **absolute lifetime of 30 days** with **inactivity lifetime 5 minutes**. Keep access tokens short (for example 10 minutes), since the server also refuses tokens issued more than 30 days ago. `POST /v1/me/sign-out` ends the session on Cairn's side. The client also calls Auth0's `/v2/logout`.

## API environment

```
CAIRN_AUTH0_DOMAIN=<tenant>.us.auth0.com        # or a custom domain
CAIRN_AUTH0_AUDIENCE=https://api.cairn.example
CAIRN_CLAIM_NAMESPACE=https://cairn.invalid/     # must match the Action secret
CAIRN_AUTH0_EMAIL_CONNECTION=email                # the passwordless connection's name
```

The issuer (`https://<domain>/`) and the signing keys (`/.well-known/jwks.json`) are derived from the domain.

## Client flow

1. Call `GET /v1/welcome` and show the acknowledgment and the three buttons with equal weight. Each one includes the `auth0_connection` to pass to Auth0's `/authorize`, so the user goes straight to Google, Apple, or the email form.
2. If the user cancels on Google's or Apple's screen, go back to the welcome screen with `?oauth_cancelled=true`.
3. After every sign-in, call `POST /v1/registrations` with the access token (plus `name_from_provider` and `time_zone` when known). A new account gets the first onboarding screen. A returning one resumes where it left off.
4. If the email already has an account created another way, the API returns 409 `account_exists` with `copy.account_exists` and a `next_step` whose primary option is the method used last time. The second option, `link_after_sign_in`, adds the new method instead (step 5).
5. To add another way to sign in (UC-REG-05), sign the user in the original way, then run Auth0's `/authorize` again with the other connection and the Cairn audience, and call `POST /v1/me/sign-in-methods` with the original access token in the header and the new access token in the body (`access_token`). Both tokens are verified. Afterwards either sign-in reaches the same Cairn account. Auth0 still sees two users. Cairn maps both to one account, so no Management API linking is needed, and deleting the account deletes both Auth0 users.

## Known limits

- **Accounts are never linked automatically across methods.** Someone who signs up with Google and later tries email with the same address is sent back to Google, with the option to add email after signing in with Google (client flow step 5).
- **Apple "Hide My Email"** gives Cairn a private relay address. A person who wants to sign in with their real address too can add it from the signed-in account (client flow step 5). If they already made a second account with it, support merges the two using `database/docs/support-account-merge.md`.
