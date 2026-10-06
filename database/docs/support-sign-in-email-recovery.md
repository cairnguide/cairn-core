# Support runbook: someone can't get into the email they use for Cairn

UC-REG-20 (account spec 3.2.0, proposed) requires a documented process before support changes the sign-in email. This is the draft for the security review. It must not be used with real users until that review signs it off. [LEGAL REVIEW REQUIRED]

Typical reasons: the address was a shared family account, it belonged to the person who died, or the person lost access to an old provider.

## Who this is for

Only accounts created with email (a passwordless magic link). For Google or Apple, access follows that Google or Apple account, and Cairn can't change it. `GET /v1/sign-in-help?method=google` or `?method=apple` sends the person to the provider's own recovery page. Support doesn't change those sign-ins.

## Steps

1. **Open a request.** The person selects "I can't get into my email" on the sign-in screen (`GET /v1/sign-in-help`) and contacts support. Record the ticket number. Never record the old or new address in the ticket body.
2. **Check it's really them, with the documented identity check.** Support asks questions only the account holder can answer from inside their own account history, without asking for anything Cairn doesn't already hold. Cairn holds very little on purpose (account spec data_boundary): no legal name, no date of birth, no phone number. So the check combines:
   - the approximate date the account was created and the sign-in method,
   - the preferred name and the voice chosen at setup,
   - how many cases the account has and roughly when each journey started (never the names of the people who died, and never read back to the caller),
   - a code sent to the new address, which the person reads back.
   Two wrong answers end the attempt. The person can try again after 24 hours. Support never accepts a screenshot, a forwarded email, or a document as proof.
3. **Tell the old address first, if it still works.** Before the change takes effect, send one notice to the old address that a change was requested, with no details about the account and a way to stop it. Wait 72 hours. If anyone answers from the old address to stop it, stop and escalate.
4. **Change the sign-in email.** An operator changes `users.email` and `users.email_lower` for that user id and creates a new Auth0 passwordless user for the new address, then deletes the old Auth0 user through the identity cleanup queue. The account id, cases, consents, and the trial don't change. Audit the change with opaque ids and the ticket number only.
5. **Confirm.** The person signs in with a magic link to the new address and checks that they see their account. Settings shows the new address. A confirmation of the change goes to the new address (UC-CASE-21).

## Never

- Change a sign-in email without the identity check in step 2 (UC-REG-20 acceptance criteria).
- Read anything about a case back to the caller.
- Leave the person without a next step: if the check can't be completed, say plainly what they can do instead, which is to start a new account. Their old account is deleted after the retention period only if nobody signs in.

## Open

- Security review of the identity check before launch (UC-REG-20 legal_review).
- Whether the 72-hour wait in step 3 is right for a family that has just lost someone, or should be shorter when the old address bounces.
