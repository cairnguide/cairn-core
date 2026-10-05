"""The scheduled jobs service that Cloudflare Cron Triggers call, and secrets read from the environment."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cairn_api import jobs
from cairn_api.config import secret_from_env


def _client(registry):
    return TestClient(jobs.create_jobs_app(registry))


def test_every_scheduled_job_the_worker_calls_exists():
    # Keep in step with JOBS_BY_CRON in cloudflare/src/index.ts.
    assert set(jobs.JOBS) == {"outbound", "identity_cleanup", "purge_stale_accounts", "purge_held_cases",
                              "purge_inactive_drafts", "expire_trials", "settle_trial_clocks"}


def test_a_job_reports_counts_only():
    r = _client({"tidy": lambda: {"affected": 3, "failed": 0}}).post("/jobs/tidy")
    assert r.status_code == 200
    assert r.json() == {"job": "tidy", "status": "ok", "affected": 3, "failed": 0}


def test_a_job_with_failures_is_a_server_error_so_the_worker_logs_it():
    r = _client({"send": lambda: {"sent": {}, "failed": {"notifications": 1}}}).post("/jobs/send")
    assert r.status_code == 500
    assert r.json()["status"] == "partial"


def test_an_unknown_job_is_not_found():
    assert _client({}).post("/jobs/nope").status_code == 404


def test_a_job_without_its_provider_is_skipped_not_failed(monkeypatch):
    for name in ("CAIRN_JOBS_MONGODB_URI", "TWILIO_SENDGRID_API_KEY", "TWILIO_SENDGRID_API_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    r = _client(jobs.JOBS).post("/jobs/outbound")
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"


def test_an_error_never_echoes_its_message():
    def boom():
        raise ValueError("jane.doe@example.test")
    r = _client({"boom": boom}).post("/jobs/boom")
    assert r.status_code == 500
    assert "jane" not in r.text
    assert r.json()["error"] == "ValueError"


def test_the_jobs_service_publishes_no_docs():
    client = _client({})
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize("value,file_value,expected", [
    ("from-env", None, "from-env"),
    (None, "from-file\n", "from-file"),
    ("from-env", "from-file", "from-env"),
    (None, None, None),
])
def test_secret_from_env_reads_the_value_or_the_file(monkeypatch, tmp_path, value, file_value, expected):
    monkeypatch.delenv("CAIRN_TEST_SECRET", raising=False)
    monkeypatch.delenv("CAIRN_TEST_SECRET_FILE", raising=False)
    if value is not None:
        monkeypatch.setenv("CAIRN_TEST_SECRET", value)
    if file_value is not None:
        path = tmp_path / "secret"
        path.write_text(file_value)
        monkeypatch.setenv("CAIRN_TEST_SECRET_FILE", str(path))
    assert secret_from_env("CAIRN_TEST_SECRET") == expected
