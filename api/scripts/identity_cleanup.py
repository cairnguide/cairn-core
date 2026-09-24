"""Run the identity cleanup queue once (UC-ACCT-01). Schedule it, for example every 15 minutes.

Connects as the OWNER role, never the app role. Every value comes from the
environment and the secret manager. Nothing here is committed.

  CAIRN_OWNER_DATABASE_URL          owner connection string
  CAIRN_AUTH0_DOMAIN                tenant or custom domain
  CAIRN_AUTH0_MGMT_CLIENT_ID        Management API client (read:users, read:user_idp_tokens, delete:users)
  CAIRN_AUTH0_MGMT_CLIENT_SECRET
  CAIRN_APPLE_CLIENT_ID             Services ID used by the Auth0 Apple connection
  CAIRN_APPLE_TEAM_ID
  CAIRN_APPLE_KEY_ID
  CAIRN_APPLE_PRIVATE_KEY_FILE      path to the ES256 .p8 key
"""
import logging
import os
import pathlib
import sys

import httpx

from cairn_api.identity_cleanup import CleanupSettings, IdentityCleanup, run_once

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    env = os.environ
    cfg = CleanupSettings(
        auth0_domain=env["CAIRN_AUTH0_DOMAIN"],
        auth0_client_id=env["CAIRN_AUTH0_MGMT_CLIENT_ID"],
        auth0_client_secret=env["CAIRN_AUTH0_MGMT_CLIENT_SECRET"],
        apple_client_id=env["CAIRN_APPLE_CLIENT_ID"],
        apple_team_id=env["CAIRN_APPLE_TEAM_ID"],
        apple_key_id=env["CAIRN_APPLE_KEY_ID"],
        apple_private_key=pathlib.Path(env["CAIRN_APPLE_PRIVATE_KEY_FILE"]).read_text(),
    )
    with httpx.Client(timeout=15) as http:
        done, failed = run_once(env["CAIRN_OWNER_DATABASE_URL"], IdentityCleanup(cfg, http))
    print(f"identity cleanup: {done} done, {failed} failed")
    sys.exit(1 if failed else 0)
