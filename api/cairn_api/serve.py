"""Container entry point. Runs the API or the jobs service on one port.

  CAIRN_PROCESS   api (default) or jobs
  PORT            8080 by default. Cloudflare Containers routes to this port.

The container sits behind the Cloudflare Worker, which terminates TLS, so
forwarded headers are trusted and the Swagger UI links use the public scheme.
"""
import os

import uvicorn

APPS = {"api": "cairn_api.main:app", "jobs": "cairn_api.jobs:app"}


def main() -> None:
    process = os.environ.get("CAIRN_PROCESS", "api")
    if process not in APPS:
        raise SystemExit(f"CAIRN_PROCESS must be one of {', '.join(APPS)}.")
    uvicorn.run(APPS[process], host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                proxy_headers=True, forwarded_allow_ips="*", access_log=False)


if __name__ == "__main__":
    main()
