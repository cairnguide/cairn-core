# Every command in README.md, in one place. Run "make help" for the list.
# The Python targets use the virtual environment that scripts/dev-setup.sh creates in .venv.
.DEFAULT_GOAL := help
VENV ?= .venv
PY := $(VENV)/bin/python
# The devcontainer's MongoDB administrator. Never point these targets at real data.
MONGO_ADMIN_URI ?= mongodb://admin:admin@localhost:27017/?replicaSet=rs0
LOGIN ?= test.user

.PHONY: help setup db-apply seed-test-db dev-token run run-jobs job test test-db coverage db-check lint openapi docker-build docker-run cf-install cf-types cf-check cf-dev cf-deploy cf-tail

help: ## List the commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-14s %s\n", $$1, $$2}'

setup: ## One-time setup: .venv, MongoDB replica set, schema, login users, templates, .env
	CAIRN_ADMIN_MONGODB_URI='$(MONGO_ADMIN_URI)' scripts/dev-setup.sh

db-apply: ## Apply database/db/schema.py to the local database (collections, validators, indexes, roles)
	CAIRN_ADMIN_MONGODB_URI='$(MONGO_ADMIN_URI)' $(PY) database/db/apply.py

seed-test-db: ## Add the test logins to the local database (development only). ARGS=--reset starts them over
	CAIRN_ADMIN_MONGODB_URI='$(MONGO_ADMIN_URI)' $(PY) database/tools/seed_test_db.py $(ARGS)

dev-token: ## Print an access token for a test login while make run is up, for example: make dev-token LOGIN=new.user
	@curl -fsS -X POST http://localhost:8000/v1/dev/token -H 'Content-Type: application/json' \
	  -d '{"username": "$(LOGIN)", "password": "$(or $(CAIRN_TEST_PASSWORD),cairn-local-test-password)"}' \
	  | $(PY) -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'

run: ## Run the API with reload on http://localhost:8000 (reads .env)
	$(VENV)/bin/uvicorn cairn_api.main:app --app-dir api --env-file .env --reload --host 0.0.0.0 --port 8000

run-jobs: ## Run the scheduled jobs service on http://localhost:8001 (reads .env)
	$(VENV)/bin/uvicorn cairn_api.jobs:app --app-dir api --env-file .env --host 127.0.0.1 --port 8001

job: ## Trigger one job on the local jobs service, for example: make job NAME=purge_held_cases
	curl -fsS -X POST http://127.0.0.1:8001/jobs/$(NAME); echo

test: ## Fast test suite, no database
	cd api && ../$(PY) -m pytest -q

test-db: ## Every API test, including the use case and security suites, on a scratch database
	cd api && CAIRN_TEST_MONGODB_URI='$(MONGO_ADMIN_URI)' ../$(PY) -m pytest -q -ra

coverage: ## Every test with a coverage report for cairn_api. Fails under the floor in api/pyproject.toml
	cd api && CAIRN_TEST_MONGODB_URI='$(MONGO_ADMIN_URI)' ../$(PY) -m pytest -q --cov --cov-report=term

db-check: ## The data security suite only (roles, validators, case boundary, jobs) on a scratch database
	cd api && CAIRN_TEST_MONGODB_URI='$(MONGO_ADMIN_URI)' ../$(PY) -m pytest -q -ra tests/test_data_security.py

lint: ## The same checks CI runs
	$(VENV)/bin/ruff check .
	shellcheck .github/scripts/*.sh scripts/*.sh
	$(VENV)/bin/actionlint
	node --check auth0/actions/*.js
	$(PY) database/tools/load_templates.py --dry-run --allow-unreviewed
	cd cloudflare && npx wrangler types >/dev/null && npx tsc --noEmit

openapi: ## Regenerate api/openapi.json after changing an endpoint or schema
	$(PY) api/scripts/export_openapi.py

docker-build: ## Build the container image Cloudflare runs
	docker build -t cairn-api:local .

docker-run: ## Run the container image locally on http://localhost:8080 (reads .env)
	docker run --rm -p 8080:8080 --env-file .env --add-host=host.docker.internal:host-gateway \
	  -e MONGODB_URI="$$(grep ^MONGODB_URI= .env | cut -d= -f2- | sed s/localhost/host.docker.internal/)" cairn-api:local

cf-install: ## Install the Worker dependencies (wrangler, @cloudflare/containers)
	cd cloudflare && npm ci

cf-types: ## Generate cloudflare/worker-configuration.d.ts (the Worker's types, gitignored)
	cd cloudflare && npx wrangler types

cf-check: ## Type-check the Worker and dry-run the deploy (no upload)
	cd cloudflare && npx wrangler types && npx tsc --noEmit && npx wrangler deploy --dry-run

cf-dev: ## Run the Worker and container locally with wrangler (needs Docker and cloudflare/.dev.vars)
	cd cloudflare && npx wrangler dev

cf-deploy: ## Build the image, push it, and deploy the Worker to Cloudflare
	cd cloudflare && npx wrangler deploy

cf-tail: ## Stream the deployed Worker's logs
	cd cloudflare && npx wrangler tail
