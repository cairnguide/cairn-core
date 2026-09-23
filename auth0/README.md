# Auth0 setup for Cairn

Cairn accounts can be created three ways: **Google**, **Apple**, or an **email address** (with a password or a passkey). Auth0 handles all three, including email confirmation, password resets, and multi-factor. Cairn never sees a password.

## Tenant configuration

1. **API.** Applications > APIs > Create API. Set the identifier to your audience (for example `https://api.cairn.example`) and the signing algorithm to RS256. This value is `CAIRN_AUTH0_AUDIENCE`.
2. **Connections.** Enable exactly these for the Cairn application and disable all others:
   - Social > **google-oauth2**, using Cairn's own Google OAuth client ID and secret, not Auth0's development keys.
   - Social > **apple**, using an Apple Services ID, Team ID, Key ID, and private key from the Apple Developer account. Apple only returns the user's name on the very first sign-in, so the client should keep it and send it in `POST /v1/registrations`.
   - Database > **Username-Password-Authentication**, or another name set as `CAIRN_AUTH0_EMAIL_CONNECTION`. Turn on email verification, a strong password policy, and optionally passkeys.
3. **Action.** Actions > Library > Build Custom > post-login. Paste `actions/cairn-claims.js`, add the secret `CLAIM_NAMESPACE` with the same value as `CAIRN_CLAIM_NAMESPACE`, then deploy it and add it to the Login flow.
4. **Application.** A single-page or native application with the three connections enabled. The client requests the Cairn audience so the access token is issued for the API.

## API environment

```
CAIRN_AUTH0_DOMAIN=<tenant>.us.auth0.com        # or a custom domain
CAIRN_AUTH0_AUDIENCE=https://api.cairn.example
CAIRN_CLAIM_NAMESPACE=https://cairn.invalid/     # must match the Action secret
CAIRN_AUTH0_EMAIL_CONNECTION=Username-Password-Authentication
```

The issuer (`https://<domain>/`) and the signing keys (`/.well-known/jwks.json`) are derived from the domain.

## Client flow

1. Call `GET /v1/sign-in-methods` and show the three buttons. Each one includes the `auth0_connection` to pass to Auth0's `/authorize`, so the user goes straight to Google, Apple, or the email form.
2. After Auth0 redirects back, call `POST /v1/registrations` with the access token, the user's name, and the accepted policy versions.
3. If the email isn't confirmed yet (email sign-up only), the API returns 403 `email_not_verified`. Auth0 sends the confirmation link.
4. If the email already has an account created another way, the API returns 409 `account_exists` with `sign_in_method` saying which method to use.

## Known limits

- **Accounts are not linked across methods.** Someone who signs up with Google and later tries email with the same address is sent back to Google. Auth0 account linking could merge the two later, as a separate decision.
- **Apple "Hide My Email"** gives Cairn a private relay address. If that person later signs up by email with their real address, Cairn can't tell it's the same person and creates a second account.
