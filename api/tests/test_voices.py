"""Cairn voices (voices/): loading, validation, provisioning, and the user's stored voice (UC-REG-12).

Tests that take the `contract_client` fixture, or no fixture, need no database. The rest
need CAIRN_TEST_ADMIN_URL (see conftest.py). Fake data only.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import re
import shutil
import uuid

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient

from cairn_api.copy_store import load_copy
from cairn_api.main import create_app
from cairn_api.schemas import Voice
from cairn_api.voices import DEFAULT_DIR, SAFETY_MODES, VoiceError, load_voices

from .conftest import DB_DIR, REPO, SETTINGS, _apply_sql, as_user, onboard

VOICES_DIR = REPO / "voices"
MIGRATION = DB_DIR / "db" / "migrations" / "0009_user_voice.sql"
MANIFEST = yaml.safe_load((VOICES_DIR / "manifest.yaml").read_text())


# ------------------------------------------------------------------ loading (no database)

def test_the_repository_voices_load():
    catalog = load_voices()
    assert DEFAULT_DIR == VOICES_DIR
    assert [v.value for v in catalog.voices] == [v["id"] for v in MANIFEST["voices"]]
    assert set(catalog.voices) == set(Voice)
    assert catalog.default == Voice.steady_direct
    assert catalog.version == MANIFEST["version"]
    assert catalog.core == (VOICES_DIR / "core.md").read_text()
    for entry in MANIFEST["voices"]:
        spec = catalog[entry["id"]]
        assert (spec.label, spec.tagline) == (entry["label"], entry["tagline"])
        assert spec.text == (VOICES_DIR / entry["file"]).read_text()
        assert spec.sample and spec.sample in spec.text


def test_manifest_paths_resolve_inside_the_voices_folder():
    """The manifest once said voices/<file>, which resolved to voices/voices/<file> and failed to load."""
    for entry in MANIFEST["voices"]:
        assert (VOICES_DIR / entry["file"]).is_file(), entry["file"]
    assert (VOICES_DIR / MANIFEST["core"]).is_file()


def test_voice_ids_match_the_database_constraint_and_default():
    sql = MIGRATION.read_text()
    listed = re.search(r"CHECK \(voice IN \(([^)]*)\)\)", sql).group(1)
    assert [v.strip().strip("'") for v in listed.split(",")] == [v.value for v in Voice]
    assert re.search(r"DEFAULT '(\w+)'", sql).group(1) == MANIFEST["default_voice"]


def test_every_voice_answers_the_same_situation():
    catalog = load_voices()
    assert {v.sample_situation for v in catalog.voices.values()} == {catalog.sample_situation}
    assert len({v.sample for v in catalog.voices.values()}) == len(Voice)


def test_voice_labels_are_not_human_first_names():
    assert [load_voices()[v].label for v in Voice] == \
        ["Steady and Direct", "Warm & Patient", "Brisk and Businesslike", "Plain and Practical"]


def test_voice_text_shown_in_onboarding_has_no_em_dashes_or_semicolons():
    for v in load_voices().voices.values():
        for text in (v.label, v.tagline, v.sample, v.sample_situation):
            assert "—" not in text and ";" not in text, (v.id, text)


def test_every_voice_has_a_confirmation():
    copy = load_copy()
    for v in Voice:
        assert copy[f"voice_{v.value}_confirm"].format(preferred_name="Pat").startswith(("Thanks, Pat",
                                                                                          "Thank you, Pat",
                                                                                          "Got it, Pat"))


# ------------------------------------------------------------------ provisioning (no database)

@pytest.mark.parametrize("voice", list(Voice))
def test_each_voice_provisions_a_system_prompt(voice):
    catalog = load_voices()
    blocks = catalog.system_blocks(voice, "normal", "case facts", "https://example.test/cite")
    assert [b["type"] for b in blocks] == ["text", "text", "text"]
    # Cache order: shared core, then the voice, then per-turn context (not cached).
    assert blocks[0] == {"type": "text", "text": catalog.core, "cache_control": {"type": "ephemeral"}}
    assert blocks[1]["text"] == f"<voice>\n{catalog[voice].text}\n</voice>"
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[2]
    assert "<safety_mode>normal</safety_mode>" in blocks[2]["text"]
    assert "case facts" in blocks[2]["text"] and "https://example.test/cite" in blocks[2]["text"]


def test_the_safety_rules_are_identical_for_every_voice():
    """A voice changes tone only. The crisis protocol lives in core.md and is the same block for everyone."""
    catalog = load_voices()
    cores = {catalog.system_blocks(v, "risk_of_harm", "", "")[0]["text"] for v in Voice}
    assert len(cores) == 1
    core = cores.pop()
    assert "988" in core and "Distress protocol" in core and "attorney" in core
    for mode in SAFETY_MODES:
        assert re.search(rf"^- {mode}\b", core, re.MULTILINE), mode


def test_unknown_voice_or_safety_mode_is_refused():
    catalog = load_voices()
    with pytest.raises(ValueError):
        catalog.system_blocks("gentle", "normal", "", "")
    with pytest.raises(ValueError):
        catalog.system_blocks(Voice.warm_patient, "calm", "", "")


def test_reference_assembler_builds_the_same_prompt():
    """voices/assemble_prompt.py loads the same files and needs no API key to build a prompt."""
    spec = importlib.util.spec_from_file_location("assemble_prompt", VOICES_DIR / "assemble_prompt.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    catalog = load_voices()
    for v in Voice:
        assert module.build_system(v.value, "normal", "ctx", "cites") == \
            catalog.system_blocks(v, "normal", "ctx", "cites")


# ------------------------------------------------------------------ validation (no database)

@pytest.fixture
def voices_copy(tmp_path):
    """A writable copy of voices/."""
    root = tmp_path / "voices"
    shutil.copytree(VOICES_DIR, root)
    return root


def write_manifest(root, change):
    manifest = yaml.safe_load((root / "manifest.yaml").read_text())
    change(manifest)
    (root / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))


def test_a_copy_of_the_folder_loads(voices_copy):
    assert set(load_voices(voices_copy).voices) == set(Voice)


def _edit_voice(voice_id, **changes):
    def change(m):
        entry = next(v for v in m["voices"] if v["id"] == voice_id)
        entry.update(changes)
    return change


@pytest.mark.parametrize(("change", "message"), [
    (_edit_voice("warm_patient", file="missing.md"), "does not exist"),
    (_edit_voice("warm_patient", file="../manifest.yaml"), "outside the voices folder"),
    (_edit_voice("warm_patient", id="gentle"), "not one of"),
    (_edit_voice("warm_patient", tagline=""), "needs a label and a tagline"),
    (_edit_voice("warm_patient", label="Warm"), "onboarding label must match"),
    (_edit_voice("warm_patient", file="steady-direct.md"), "id line must say warm_patient"),
    (lambda m: m["voices"].pop(), "no entry for ['plain_practical']"),
    (lambda m: m["voices"].append(dict(m["voices"][0])), "listed twice"),
    (lambda m: m.update(default_voice="gentle"), "default_voice"),
    (lambda m: m.update(core="nope.md"), "core file nope.md does not exist"),
    (lambda m: m.update(safety_modes=["normal"]), "safety_modes"),
    (lambda m: m.pop("voices"), "needs a voices list"),
])
def test_a_broken_manifest_is_refused(voices_copy, change, message):
    write_manifest(voices_copy, change)
    with pytest.raises(VoiceError) as e:
        load_voices(voices_copy)
    assert message in str(e.value)


def test_a_voice_file_without_a_reference_response_is_refused(voices_copy):
    path = voices_copy / "plain-practical.md"
    path.write_text(path.read_text().split("## Reference response")[0])
    with pytest.raises(VoiceError, match="reference response"):
        load_voices(voices_copy)


def test_reference_responses_must_share_one_situation(voices_copy):
    path = voices_copy / "brisk-businesslike.md"
    path.write_text(path.read_text().replace('User: "My mom died Tuesday.', 'User: "My dad died Tuesday.'))
    with pytest.raises(VoiceError, match="same user message"):
        load_voices(voices_copy)


def test_an_empty_voice_file_is_refused(voices_copy):
    (voices_copy / "core.md").write_text("  \n")
    with pytest.raises(VoiceError, match="core.md: file is empty"):
        load_voices(voices_copy)


def test_a_missing_folder_is_refused(tmp_path):
    with pytest.raises(VoiceError, match="CAIRN_VOICES_DIR"):
        load_voices(tmp_path)


def test_the_app_does_not_start_with_broken_voices(voices_copy):
    from .conftest import NoDatabase
    write_manifest(voices_copy, lambda m: m["voices"].pop())
    app = create_app(settings=dataclasses.replace(SETTINGS, voices_dir=str(voices_copy)),
                     database=NoDatabase(), verifier=object())
    with pytest.raises(VoiceError), TestClient(app):
        pass


# ------------------------------------------------------------------ contract (no database)

def test_the_contract_lists_the_voices(contract_client):
    schemas = contract_client.app.openapi()["components"]["schemas"]
    assert schemas["Voice"]["enum"] == [v.value for v in Voice]
    assert "personality" not in schemas["AccountOut"]["properties"]
    assert "voice" in schemas["AccountOut"]["required"]


@pytest.mark.parametrize("choice", ["gentle", "steady", "straightforward", "Warm & Patient", ""])
def test_an_unknown_voice_choice_is_rejected(contract_client, choice):
    r = contract_client.put("/v1/onboarding/personality", json={"choice": choice}, headers=as_user("x"))
    assert r.status_code == 422


@pytest.mark.parametrize("body", [{"voice": "gentle"}, {"voice": None}, {"personality": "steady"}])
def test_settings_reject_an_unknown_or_cleared_voice(contract_client, body):
    assert contract_client.patch("/v1/me", json=body, headers=as_user("x")).status_code == 422


# ------------------------------------------------------------------ the user's voice (database)

def _voice_of(api, subject):
    with psycopg.connect(api.scratch_url, autocommit=True) as conn:
        return conn.execute("SELECT voice FROM cairn.users WHERE idp_subject = %s", (subject,)).fetchone()[0]


@pytest.mark.parametrize("voice", list(Voice))
def test_the_chosen_voice_is_saved_on_the_user(api, voice):
    subject = f"email|voice-{voice.value}"
    r = api.post("/v1/registrations", json={}, headers=as_user(subject))
    assert r.status_code == 201, r.text
    body = onboard(api, subject, preferred_name="Pat", voice=voice.value)
    assert body["account"]["voice"] == voice.value
    assert body["screen"]["acknowledgment"] == load_copy()[f"voice_{voice.value}_confirm"].format(preferred_name="Pat")
    assert _voice_of(api, subject) == voice.value


def test_choose_for_me_saves_the_default_voice(api):
    subject = "email|voice-default"
    api.post("/v1/registrations", json={}, headers=as_user(subject))
    onboard(api, subject, voice="choose_for_me")
    assert _voice_of(api, subject) == load_voices().default.value


def test_the_stored_voice_provisions_the_prompt_and_follows_settings(api):
    subject = "email|voice-provision"
    h = as_user(subject)
    api.post("/v1/registrations", json={}, headers=h)
    onboard(api, subject, voice="warm_patient")
    catalog = api.app.state.voices
    stored = _voice_of(api, subject)
    assert catalog.system_blocks(stored, "normal", "", "")[1]["text"] == \
        f"<voice>\n{catalog['warm_patient'].text}\n</voice>"

    assert api.patch("/v1/me", json={"voice": "brisk_businesslike"}, headers=h).json()["account"]["voice"] == \
        "brisk_businesslike"
    stored = _voice_of(api, subject)
    assert stored == "brisk_businesslike"
    assert "Brisk and Businesslike" in catalog.system_blocks(stored, "normal", "", "")[1]["text"]


def test_the_database_refuses_a_voice_outside_the_manifest(api):
    subject = "email|voice-refused"
    api.post("/v1/registrations", json={}, headers=as_user(subject))
    with psycopg.connect(api.scratch_url, autocommit=True) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE cairn.users SET voice = 'gentle' WHERE idp_subject = %s", (subject,))


# ------------------------------------------------------------------ migration 0009 (database)

@pytest.fixture
def pre_voice_db():
    """A scratch database with migrations up to 0008, before users.voice existed."""
    import os
    from urllib.parse import urlsplit
    admin_url = os.environ.get("CAIRN_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set CAIRN_TEST_ADMIN_URL to run integration tests against a scratch Postgres server.")
    name = f"cairn_voice_mig_{uuid.uuid4().hex[:8]}"
    u = urlsplit(admin_url)
    url = f"{u.scheme}://{u.netloc}/{name}" + (f"?{u.query}" if u.query else "")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for f in sorted((DB_DIR / "db" / "migrations").glob("*.sql")):
                if f.name < "0009":
                    _apply_sql(conn, f)
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_migration_keeps_each_existing_choice_at_the_closest_voice(pre_voice_db):
    with psycopg.connect(pre_voice_db, autocommit=True) as conn:
        for p in ("gentle", "steady", "straightforward"):
            conn.execute("INSERT INTO cairn.users (idp_subject, email, personality) VALUES (%s, %s, %s)",
                         (f"email|mig-{p}", f"mig-{p}@example.test", p))
        _apply_sql(conn, MIGRATION)
        rows = dict(conn.execute("SELECT idp_subject, voice FROM cairn.users").fetchall())
        assert rows == {"email|mig-gentle": "warm_patient", "email|mig-steady": "steady_direct",
                        "email|mig-straightforward": "plain_practical"}
        columns = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'cairn' "
            "AND table_name = 'users'").fetchall()}
        assert "voice" in columns and "personality" not in columns
        assert conn.execute("SELECT has_column_privilege('cairn_app', 'cairn.users', 'voice', 'UPDATE')"
                            ).fetchone()[0]
