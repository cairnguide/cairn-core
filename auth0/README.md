# Auth0 setup for Cairn

Cairn accounts can be created three ways: **Google**, **Apple**, or an **email address**. Email uses a passwordless magic link by default (spec decision D-10). Auth0 handles all three, including email verification and multi-factor. Cairn never sees a password.

The requirements come from `database/docs/cairn-registration-use-cases.json` (UC-REG-02 to UC-REG-05, UC-ACCT-01).

## Tenant configuration

1. **API.** Applications > APIs > Create API. Set the identifier to your audience (for example `https://api.cairn.example`) and the signing algorithm to RS256. This value is `CAIRN_AUTH0_AUDIENCE`.
2. **Connections.** Enable exactly these for the Cairn application and disable all others:
   - Social > **google-oauth2**, using Cairn's own Google OAuth client ID and secret, not Auth0's development keys.
   - Social > **apple**, using an Apple Services ID, Team ID, Key ID, and private key from the Apple Developer account. Apple only returns the user's name on the very first sign-in, so the client should send it as `name_from_provider` in that first `POST /v1/registrations`. It's kept only to pre-fill the preferred name question, and the user confirms it.
   - **Apple Private Email Relay.** In the Apple Developer account, register Cairn's outbound email domain and sending addresses under Sign in with Apple for Email Communication, and authenticate the domain with SPF and DKIM. Then confirm that verification, trial reminder, and support emails reach a `@privaterelay.appleid.com` address. Cairn sends at most two trial reminders per account, well under Apple's limit of 100 emails per day per relay address.
   - Passwordless > **Email** (connection name `email`, the default `CAIRN_AUTH0_EMAIL_CONNECTION`). Use **link** mode, set the OTP expiry to **900 seconds** (15 minutes, UC-REG-04), and send from Cairn's own email domain (see Apple relay below). Auth0 links are single use. The client starts it with `/passwordless/start` and `send: "link"`. It shows "Send a new link" when a link has expired, and offers Resend plus a spelling check after 60 seconds.
   - Passwords are optional. If a database connection is also enabled, follow NIST SP 800-63B-4: minimum length **15** when the password is the only factor, allow at least **64** characters (check the tenant's maximum), password policy **None** (no composition rules), **Breached Password Detection** on, and no forced periodic rotation.
3. **Action.** Actions > Library > Build Custom > post-login. Paste `actions/cairn-claims.js`, add the secret `CLAIM_NAMESPACE` with the same value as `CAIRN_CLAIM_NAMESPACE`, then deploy it and add it to the Login flow.
4. **Management API client for account deletion.** Applications > Machine to Machine, authorized for the Auth0 Management API with `read:users`, `read:user_idp_tokens`, and `delete:users`. The identity cleanup worker (`api/cairn_api/identity_cleanup.py`) uses it to delete the Auth0 user and, for Apple, to read the refresh token it revokes with Apple. Keep its secret and the Apple private key in the secret manager.
5. **Application.** A single-page or native application with the three connections enabled. The client requests the Cairn audience so the access token is issued for the API.

## API environment

```
CAIRN_AUTH0_DOMAIN=<tenant>.us.auth0.com        # or a custom domain
CAIRN_AUTH0_AUDIENCE=https://api.cairn.example
CAIRN_CLAIM_NAMESPACE=https://cairn.invalid/     # must match the Action secret
CAIRN_AUTH0_EMAIL_CONNECTION=Username-Password-Authentication
```

The issuer (`https://<domain>/`) and the signing keys (`/.well-known/jwks.json`) are derived from the domain.

## Client flow

1. Call `GET /v1/welcome` and show the acknowledgment and the three buttons with equal weight. Each one includes the `auth0_connection` to pass to Auth0's `/authorize`, so the user goes straight to Google, Apple, or the email form.
2. If the user cancels on Google's or Apple's screen, go back to the welcome screen with `?oauth_cancelled=true`.
3. After every sign-in, call `POST /v1/registrations` with the access token (plus `name_from_provider` and `time_zone` when known). A new account gets the first onboarding screen. A returning one resumes where it left off.
4. If the email already has an account created another way, the API returns 409 `account_exists` with `copy.account_exists` and a `next_step` whose primary option is the method used last time.

## Known limits

- **Accounts are never linked automatically across methods.** Someone who signs up with Google and later tries email with the same address is sent back to Google. Linking a second method after signing in with the first (UC-REG-05) is not built yet.
- **Apple "Hide My Email"** gives Cairn a private relay address. If that person later signs up by email with their real address, Cairn sees two accounts. Support merges them using `database/docs/support-account-merge.md`.
