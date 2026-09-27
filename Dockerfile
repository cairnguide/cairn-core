# Cairn API container image. Cloudflare Containers runs it behind the Worker in cloudflare/.
# The same image runs the API (CAIRN_PROCESS=api, the default) or the scheduled jobs service
# (CAIRN_PROCESS=jobs). Nothing secret is baked in: every setting arrives as an environment variable.
#
#   docker build -t cairn-api .
#   docker run --rm -p 8080:8080 --env-file .env cairn-api
#
# Cloudflare Containers run linux/amd64. wrangler builds for that platform. For a manual build,
# add --platform linux/amd64.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    CAIRN_PROCESS=api \
    CAIRN_VOICES_DIR=/app/voices

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY api/pyproject.toml api/pyproject.toml
COPY api/cairn_api/__init__.py api/cairn_api/__init__.py
RUN pip install ./api && pip uninstall -y cairn-api

COPY api/ api/
COPY voices/ voices/
# Read-only for the runtime user, whatever file modes the checkout had.
RUN pip install --no-deps ./api \
    && chmod -R a+rX,go-w /app \
    && useradd --system --uid 10001 --no-create-home cairn

USER cairn
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/healthz\", timeout=4)"]

CMD ["python", "-m", "cairn_api.serve"]
