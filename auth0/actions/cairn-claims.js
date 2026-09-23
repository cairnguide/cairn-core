/**
 * Auth0 post-login Action: adds the claims the Cairn API reads to the access token.
 *
 * Auth0 access tokens don't carry email by default, so this Action adds three
 * namespaced claims. The namespace must match CAIRN_CLAIM_NAMESPACE on the API.
 *
 *   <ns>email            the user's email (for Apple, possibly a private relay address)
 *   <ns>email_verified   true only once Google, Apple, or Auth0's own email check has confirmed it
 *   <ns>sign_in_method   the connection strategy: google-oauth2, apple, auth0, or email
 *
 * Set the CLAIM_NAMESPACE secret on the Action. No personal data is logged here.
 */
exports.onExecutePostLogin = async (event, api) => {
  const ns = event.secrets.CLAIM_NAMESPACE || 'https://cairn.invalid/';

  // Only the three supported methods may reach the API. The API enforces this
  // too. Stopping it here gives the user a clear message at sign-in.
  const allowed = ['google-oauth2', 'apple', 'auth0', 'email'];
  if (!allowed.includes(event.connection.strategy)) {
    api.access.deny('Please sign in with Google, Apple, or your email address.');
    return;
  }

  if (event.user.email) {
    api.accessToken.setCustomClaim(`${ns}email`, event.user.email);
  }
  api.accessToken.setCustomClaim(`${ns}email_verified`, event.user.email_verified === true);
  api.accessToken.setCustomClaim(`${ns}sign_in_method`, event.connection.strategy);
};
