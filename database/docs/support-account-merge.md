# Support runbook: merging an Apple relay account with a real-email account

UC-REG-05 requires a documented support path for this case. Cairn never links accounts automatically. A person who signed up with Sign in with Apple and "Hide My Email" has a `@privaterelay.appleid.com` address. If they later sign up with their real email, Cairn sees two unrelated accounts.

**Most people can fix this themselves.** Signed in to the account they want to keep, they can add another way to sign in (`POST /v1/me/sign-in-methods`, UC-REG-05). That works as long as the other sign-in doesn't already have its own Cairn account. If it does and that account is empty, they delete it from Settings first, then add the sign-in. Support is needed only when they can't do that, or when both accounts have cases.

## When to use this

Only when the person contacts support, and only after both accounts are verified as theirs.

## Steps

1. **Verify both identities.** Ask the person to sign in with each method while on the support call or chat, and to read back the account id shown under Settings for each. Support never accepts a screenshot or a forwarded email as proof.
2. **Choose the account to keep.** Usually the one that has cases. If both have cases, stop and escalate: case membership merges are not supported at MVP.
3. **Remove the empty Cairn account.** Have the person sign in to the account being removed and use Settings, then Delete account. If they can't, an operator runs the deletion for that user id. Deleting also queues the Auth0 and Apple cleanup (UC-REG-15), so that sign-in starts fresh next time.
4. **Add the sign-in to the kept account.** Have the person sign in to the kept account, then add the other method from Settings (`POST /v1/me/sign-in-methods`). Cairn records it under `users.linked_identities`. No Auth0 Management API linking is needed: Auth0 keeps two users, and Cairn maps both to one account. Record the ticket number, not personal details.
5. **Confirm.** Ask the person to sign in with both methods and check that they land in the same account. Settings lists the added method under `linked_sign_in_methods`.

## Notes

- Consent records are never copied between accounts. The kept account's acknowledgments stand. If they are out of date, the person is asked again on next sign-in (UC-REG-13).
- The trial is per account and is not reset by a merge (D-04).
- Log opaque ids and the ticket number only. Never log names or email addresses.

[LEGAL REVIEW REQUIRED] Confirm the identity verification standard for step 1.
