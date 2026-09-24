# Support runbook: merging an Apple relay account with a real-email account

UC-REG-05 requires a documented support path for this case. Cairn never links accounts automatically. A person who signed up with Sign in with Apple and "Hide My Email" has a `@privaterelay.appleid.com` address. If they later sign up with their real email, Cairn sees two unrelated accounts.

## When to use this

Only when the person contacts support, and only after both accounts are verified as theirs.

## Steps

1. **Verify both identities.** Ask the person to sign in with each method while on the support call or chat, and to read back the account id shown under Settings for each. Support never accepts a screenshot or a forwarded email as proof.
2. **Choose the account to keep.** Usually the one that has cases. If both have cases, stop and escalate: case membership merges are not supported at MVP.
3. **Link in Auth0.** In the Auth0 Management API, link the other identity to the kept user (`POST /api/v2/users/{kept_id}/identities`). The linked identity then signs in to the kept account. Record the ticket number, not personal details.
4. **Remove the empty Cairn account.** Have the person sign in to the account being removed and use Settings, then Delete account. If they can't, an operator runs the deletion as the owner role for that user id. Deleting also queues the Auth0 and Apple cleanup (UC-ACCT-01). Skip that cleanup for the identity that was just linked.
5. **Confirm.** Ask the person to sign in with both methods and check that they land in the same account.

## Notes

- Consent records are never copied between accounts. The kept account's acknowledgments stand. If they are out of date, the person is asked again on next sign-in (UC-REG-13).
- The trial is per account and is not reset by a merge (D-04).
- Log opaque ids and the ticket number only. Never log names or email addresses.

[LEGAL REVIEW REQUIRED] Confirm the identity verification standard for step 1.
