"""Run the identity cleanup queue once (UC-ACCT-01). Schedule it, for example every 15 minutes.

Connects as the cairnJobs MongoDB user, never the app user. Every value comes from the
environment and the secret manager. Nothing here is committed. On Cloudflare the
same job runs through cairn_api.jobs instead (see the repository README).

  CAIRN_JOBS_MONGODB_URI            the cairnJobs user's connection string
  CAIRN_MONGODB_DB                  the Cairn database name, cairn by default
  CAIRN_AUTH0_DOMAIN                tenant or custom domain
  CAIRN_AUTH0_MGMT_CLIENT_ID        Management API client (read:users, read:user_idp_tokens, delete:users)
  CAIRN_AUTH0_MGMT_CLIENT_SECRET    (or CAIRN_AUTH0_MGMT_CLIENT_SECRET_FILE)
  CAIRN_APPLE_CLIENT_ID             Services ID used by the Auth0 Apple connection
  CAIRN_APPLE_TEAM_ID
  CAIRN_APPLE_KEY_ID
  CAIRN_APPLE_PRIVATE_KEY_FILE      path to the ES256 .p8 key (or CAIRN_APPLE_PRIVATE_KEY, the key itself)
"""
import logging
import sys

import httpx

from cairn_api.identity_cleanup import IdentityCleanup, run_once
from cairn_api.jobs import cleanup_settings_from_env, jobs_database

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = cleanup_settings_from_env()
    with httpx.Client(timeout=15) as http:
        done, failed = run_once(jobs_database(), IdentityCleanup(cfg, http))
    print(f"identity cleanup: {done} done, {failed} failed")
    sys.exit(1 if failed else 0)
