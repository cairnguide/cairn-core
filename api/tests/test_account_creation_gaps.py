"""Gaps closed by the account creation review (database/docs/account-creation-review-gap-audit.md).

UC-REG-04  the copy around the email link is on the welcome screen
UC-REG-05  adding a second way to sign in, only after signing in the original way
UC-REG-10, UC-REG-13  the scheduled job that deletes unfinished sign-ups after 90 days (D-2026-10-05-R1)
UC-REG-14  sign-up reads free text with the crisis plan's detector, the same one case creation uses. Naming a
           death by suicide is a loss, not a crisis (D-2026-10-05-S1)
UC-REG-15, UC-REG-16  deleting and downloading cover linked sign-ins too

The first section needs no database. The rest run against a scratch MongoDB (tests/conftest.py).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from cairn_api import jobs, onboarding
from cairn_api.auth import Identity
from cairn_api.copy_store import load_copy
from cairn_api.errors import ApiError
from cairn_api.schemas import SignInMethod

from .conftest import as_user, onboard

COPY = load_copy()


# ------------------------------------------------------------------ no database

def test_uc_reg_04_welcome_carries_the_email_link_copy(contract_client):
    body = contract_client.get("/v1/welcome").json()["email_sign_in"]
    assert body == {"check_inbox": COPY["email_check_inbox"], "link_lifetime_minutes": 15,
                    "link_expired": COPY["email_link_expired"], "send_new_link": COPY["send_new_link"],
                    "resend_after_seconds": 60, "check_spelling": COPY["email_check_spelling"],
                    "resend": COPY["resend_link"]}


@pytest.mark.parametrize("text", [
    "I want to die",                       # the old sign-up list still pauses
    "I can't do this anymore",             # DEC-26-05, ending language not about the paperwork
    "I'm done",
    "I can't stop crying",                 # level 3, acute distress
])
def test_uc_reg_14_sign_up_pauses_on_what_the_crisis_plan_calls_level_3_or_4(text):
    assert onboarding.shows_distress(text)


@pytest.mark.parametrize("text", [
    "Pat", "Mary-Kate", None, "",
    "I'm done with these questions for now",  # clearly about the paperwork (DEC-26-05)
    "My brother died by suicide",            # a loss, not a crisis (D-2026-10-05-S1)
])
def test_uc_reg_14_ordinary_answers_do_not_pause(text):
    assert not onboarding.shows_distress(text)


# ------------------------------------------------------------------ UC-REG-05 linking

class TokenBook:
    """Stands in for Auth0 token verification of the second sign-in's token, in tests only."""

    def __init__(self, identities: dict[str, Identity]):
        self.identities = identities

    def verify(self, token: str) -> Identity:
        if token not in self.identities:
            raise ApiError(401, "invalid_token", "Please sign in again.")
        return self.identities[token]


@pytest.fixture
def tokens(api):
    book = TokenBook({})
    before = api.app.state.token_verifier
    api.app.state.token_verifier = book
    yield book.identities
    api.app.state.token_verifier = before


def _identity(subject: str, email: str, method: str, verified: bool = True) -> Identity:
    return Identity(subject=subject, email=email, email_verified=verified, sign_in_method=SignInMethod(method))


def _google_account(api, tag: str) -> tuple[str, str, dict]:
    subject, email = f"google-oauth2|{tag}", f"{tag}@example.test"
    h = as_user(subject, email, method="google")
    assert api.post("/v1/registrations", json={}, headers=h).status_code == 201
    onboard(api, subject, method="google")
    return subject, email, h


def test_uc_reg_05_account_exists_offers_the_old_method_then_linking(api):
    subject, email, _ = _google_account(api, "link-offer")
    r = api.post("/v1/registrations", json={}, headers=as_user("email|link-offer", email, method="email"))
    assert r.status_code == 409
    options = r.json()["next_step"]["options"]
    assert options[0]["value"] == "google"  # still the primary button
    assert options[1] == {"value": "link_after_sign_in",
                          "label": COPY["link_after_sign_in"].format(provider="Google", new_provider="email"),
                          "available": True, "unavailable_reason": None}
    assert api.db.users.count_documents({"email_lower": email}) == 1  # never linked automatically


def test_uc_reg_05_linking_needs_the_original_sign_in_then_both_reach_one_account(api, tokens):
    subject, email, h = _google_account(api, "link-ok")
    tokens["email-token"] = _identity("email|link-ok", email, "email")

    # Not signed in the original way: nothing to link to.
    r = api.post("/v1/me/sign-in-methods", json={"access_token": "email-token"},
                 headers=as_user("email|link-ok", email, method="email"))
    assert r.status_code == 403 and r.json()["code"] == "registration_required"

    r = api.post("/v1/me/sign-in-methods", json={"access_token": "email-token"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "linked"
    assert r.json()["message"] == COPY["sign_in_method_linked"].format(provider="email")
    assert r.json()["account"]["linked_sign_in_methods"] == ["email"]

    # The email sign-in now opens the same account and resumes there, and the account email doesn't move.
    eh = as_user("email|link-ok", email, method="email")
    assert api.post("/v1/registrations", json={}, headers=eh).status_code == 200
    assert api.get("/v1/me", headers=eh).json()["account"]["id"] == api.get("/v1/me", headers=h).json()["account"]["id"]
    assert api.db.users.count_documents({}) == api.db.users.count_documents({"idp_subject": {"$ne": "email|link-ok"}})

    again = api.post("/v1/me/sign-in-methods", json={"access_token": "email-token"}, headers=h)
    assert again.status_code == 200 and again.json()["result"] == "already_linked"
    audit = [a["action"] for a in api.db.audit_events.find({"action": "sign_in_method_linked"})]
    assert len(audit) >= 1


def test_uc_reg_05_a_relay_address_and_a_real_address_can_be_one_account(api, tokens):
    """The case support used to merge by hand (support-account-merge.md): Apple Hide My Email, then real email."""
    subject = "apple|link-relay"
    h = as_user(subject, "x7k2@privaterelay.appleid.com", method="apple")
    assert api.post("/v1/registrations", json={}, headers=h).status_code == 201
    tokens["real"] = _identity("email|link-relay", "pat.real@example.test", "email")
    assert api.post("/v1/me/sign-in-methods", json={"access_token": "real"}, headers=h).json()["result"] == "linked"
    # Signing up again with the real address is told about the account, not given a second one.
    r = api.post("/v1/registrations", json={}, headers=as_user("google-oauth2|relay2", "pat.real@example.test",
                                                                method="google"))
    assert r.status_code == 409 and r.json()["sign_in_method"] == "email"


def test_uc_reg_05_refusals(api, tokens):
    _, _, h = _google_account(api, "link-no")
    other_subject, other_email, _ = _google_account(api, "link-other")

    tokens["unverified"] = _identity("email|unverified-link", "new@example.test", "email", verified=False)
    r = api.post("/v1/me/sign-in-methods", json={"access_token": "unverified"}, headers=h)
    assert r.status_code == 403 and r.json()["code"] == "email_not_verified"

    tokens["second-google"] = _identity("google-oauth2|link-no-2", "g2@example.test", "google")
    r = api.post("/v1/me/sign-in-methods", json={"access_token": "second-google"}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "same_method"

    tokens["someone-else"] = _identity(other_subject, other_email, "google")
    r = api.post("/v1/me/sign-in-methods", json={"access_token": "someone-else"}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "sign_in_method_in_use"

    tokens["their-email"] = _identity("email|link-other", other_email, "email")
    r = api.post("/v1/me/sign-in-methods", json={"access_token": "their-email"}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "sign_in_method_in_use"

    r = api.post("/v1/me/sign-in-methods", json={"access_token": "forged"}, headers=h)
    assert r.status_code == 401
    assert "linked_identities" not in api.db.users.find_one({"idp_subject": "google-oauth2|link-no"})


def test_uc_reg_05_a_sign_in_cannot_belong_to_two_accounts_even_past_the_api(api):
    """The unique index backs store.link_identity up."""
    from pymongo.errors import DuplicateKeyError
    _google_account(api, "uq-a")
    _google_account(api, "uq-b")
    entry = {"idp_subject": "email|uq", "sign_in_method": "email", "email_lower": "uq@example.test"}
    from datetime import datetime, timezone
    entry["linked_at"] = datetime.now(timezone.utc)
    api.db.users.update_one({"idp_subject": "google-oauth2|uq-a"}, {"$push": {"linked_identities": entry}})
    with pytest.raises(DuplicateKeyError):
        api.db.users.update_one({"idp_subject": "google-oauth2|uq-b"}, {"$push": {"linked_identities": entry}})


def test_uc_reg_05_unlinking_keeps_the_original_and_cleans_up_the_identity(api, tokens):
    _, email, h = _google_account(api, "unlink")
    tokens["e"] = _identity("email|unlink", email, "email")
    api.post("/v1/me/sign-in-methods", json={"access_token": "e"}, headers=h)

    r = api.delete("/v1/me/sign-in-methods/google", headers=h)
    assert r.status_code == 409 and r.json()["code"] == "sign_in_method_cannot_remove"

    r = api.delete("/v1/me/sign-in-methods/email", headers=h)
    assert r.status_code == 200 and r.json()["account"]["linked_sign_in_methods"] == []
    assert api.db.identity_deletion_requests.find_one({"idp_subject": "email|unlink"})["provider"] == "email"
    assert api.get("/v1/me", headers=as_user("email|unlink", email)).status_code == 403


def test_uc_reg_15_and_16_cover_linked_sign_ins(api, tokens):
    _, email, h = _google_account(api, "linked-delete")
    tokens["a"] = _identity("apple|linked-delete", "relay-ld@privaterelay.appleid.com", "apple")
    api.post("/v1/me/sign-in-methods", json={"access_token": "a"}, headers=h)

    export = api.get("/v1/me/data-export/file", headers=h)
    assert export.status_code == 200, export.text
    assert export.json()["profile"]["linked_sign_in_methods"] == ["apple"]
    assert "apple|linked-delete" not in export.text  # identity provider ids are never exported

    r = api.post("/v1/me/deletion", json={"confirm": True}, headers=h)
    assert r.status_code == 200, r.text
    queued = {(q["idp_subject"], q["provider"]) for q in api.db.identity_deletion_requests.find(
        {"idp_subject": {"$in": ["google-oauth2|linked-delete", "apple|linked-delete"]}})}
    assert queued == {("google-oauth2|linked-delete", "google"), ("apple|linked-delete", "apple")}


# ------------------------------------------------------------------ UC-REG-10 and UC-REG-13 cleanup job

@pytest.fixture
def jobs_on_scratch(api, monkeypatch):
    monkeypatch.setattr(jobs, "jobs_database", lambda: api.jobs_db)
    for name in ("CAIRN_PENDING_ACCOUNT_RETENTION_DAYS", "CAIRN_NO_CASE_ACCOUNT_RETENTION_DAYS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _age(api, subject: str, days: int) -> None:
    u = api.db.users.find_one({"idp_subject": subject})
    api.db.users.update_one({"_id": u["_id"]}, {"$set": {"created_at": u["created_at"] - timedelta(days=days)}})


def test_uc_reg_10_unfinished_sign_ups_have_90_days(api, jobs_on_scratch):
    """D-2026-10-05-R1. With nothing set, the job runs with 90 days: day 91 is deleted, day 89 isn't."""
    assert jobs.PENDING_ACCOUNT_RETENTION_DAYS == 90
    old, recent = "email|stale-91", "email|stale-89"
    for subject, days in ((old, 91), (recent, 89)):
        assert api.post("/v1/registrations", json={}, headers=as_user(subject)).status_code == 201
        _age(api, subject, days)
    from fastapi.testclient import TestClient
    r = TestClient(jobs.create_jobs_app()).post("/jobs/purge_stale_accounts")
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    assert api.db.users.find_one({"idp_subject": old}) is None
    assert api.db.users.find_one({"idp_subject": recent}) is not None


def test_uc_reg_10_and_13_unfinished_sign_ups_are_deleted_after_the_set_period(api, jobs_on_scratch):
    pending = "email|stale-pending"
    assert api.post("/v1/registrations", json={}, headers=as_user(pending)).status_code == 201
    no_case = "email|stale-no-case"
    api.post("/v1/registrations", json={}, headers=as_user(no_case))
    onboard(api, no_case)
    fresh = "email|stale-fresh"
    api.post("/v1/registrations", json={}, headers=as_user(fresh))
    for s in (pending, no_case):
        _age(api, s, 40)

    jobs_on_scratch.setenv("CAIRN_PENDING_ACCOUNT_RETENTION_DAYS", "30")
    jobs.purge_stale_accounts()
    assert api.db.users.find_one({"idp_subject": pending}) is None
    assert api.db.users.find_one({"idp_subject": no_case}) is not None  # its period isn't set
    assert api.db.users.find_one({"idp_subject": fresh}) is not None
    assert api.db.identity_deletion_requests.find_one({"idp_subject": pending})

    jobs_on_scratch.setenv("CAIRN_NO_CASE_ACCOUNT_RETENTION_DAYS", "30")
    jobs.purge_stale_accounts()
    assert api.db.users.find_one({"idp_subject": no_case}) is None


def test_uc_reg_10_a_zero_day_period_is_refused(api, jobs_on_scratch):
    jobs_on_scratch.setenv("CAIRN_PENDING_ACCOUNT_RETENTION_DAYS", "0")
    with pytest.raises(RuntimeError):
        jobs.purge_stale_accounts()
