# Cairn database (MongoDB)

The Cairn MVP data layer: the MongoDB schema with its validators, indexes, and roles, a content-as-code template loader, and the tools to provision users and move an existing PostgreSQL database across. The API's data-access layer is `api/cairn_api/store.py` and its security suite is `api/tests/test_data_security.py`.

Start with [CLAUDE.md](CLAUDE.md) for context, decisions, invariants, and open questions.

## Layout

```
db/schema.py        collections, $jsonSchema and $expr validators, indexes, roles, and settings
db/apply.py         makes a database match schema.py (safe to run again)
tools/load_templates.py      validates and loads task and journey templates (as the cairnLoader user)
tools/create_login_user.py   creates a login user with exactly one Cairn role (self-managed only)
tools/move_from_postgres.py  one-time copy of a Cairn PostgreSQL database into MongoDB
content/            template and journey files, and their JSON Schemas
docs/               specs, data model, and gap audits
```

## Requirements

- MongoDB 7.0 or newer, as a **replica set**. Every API request is one multi-document transaction, and MongoDB only runs those on a replica set or sharded cluster ([Transactions](https://www.mongodb.com/docs/manual/core/transactions/)). Every Atlas cluster is a replica set. CI tests 7.0 and 8.0.
- Authentication on, so the roles below mean something.

## Who connects as what

| Login | Role | Used by | Can |
|---|---|---|---|
| `cairn_api` | `cairnApp` | The API container (`MONGODB_URI`) | Read and write user and case data. Append audit rows and never read them. Queue identity cleanup and confirmations and never read them. Read templates and settings. |
| `cairn_jobs` | `cairnJobs` | The jobs container (`CAIRN_JOBS_MONGODB_URI`) | Purge, claim, and send. Append to the audit and confirmation logs and never change them. No access to templates. |
| `cairn_loader` | `cairnLoader` | The deploy workflow (`CAIRN_LOADER_MONGODB_URI`) | Add template versions and flip their `active` flag. Nothing else. |
| an administrator | Atlas admin, or dbAdmin and userAdmin | `db/apply.py`, `tools/move_from_postgres.py` (`CAIRN_ADMIN_MONGODB_URI`) | Everything. Never given to a running service. |

`ROLES` in `db/schema.py` is the exact privilege list. None of the roles has `bypassDocumentValidation`, so the validators bind every Cairn login.

## Atlas setup (production and staging)

1. Create an M10 or larger dedicated cluster in the region the data must stay in, with encryption at rest. Turn on backups and practice a restore.
2. Create the three custom roles. Atlas manages roles and users outside the database, so `createRole` and `createUser` are refused from a driver ([Unsupported Commands](https://www.mongodb.com/docs/atlas/unsupported-commands/)). Print the role definitions in the Atlas Admin API format, then add them in the Atlas UI (Database Access, Custom Roles) or through the Admin API or Atlas CLI ([Configure Custom Database Roles](https://www.mongodb.com/docs/atlas/security-add-mongodb-roles/)):

   ```bash
   python3 database/db/apply.py --print-roles --db cairn
   ```

   Run this again after any change to `ROLES` in `db/schema.py`, and update the roles in Atlas to match.
3. Create one database user per row of the table above, each with only its one custom role ([Configure Database Users](https://www.mongodb.com/docs/atlas/security-add-mongodb-users/)). Keep the passwords in the secret manager. Never commit them.
4. Network access. Atlas only accepts connections from its IP access list ([IP Access List](https://www.mongodb.com/docs/atlas/security/ip-access-list/)). Cloudflare Containers don't have fixed outbound IP addresses (see the repository README), so the list has to admit them broadly, relying on TLS and the per-role passwords. **[DECISION NEEDED]** whether to put a fixed-egress proxy in front before production (`CLAUDE.md`, open question 12).
5. Apply the schema and load the templates. The deploy workflow does both. By hand:

   ```bash
   CAIRN_ADMIN_MONGODB_URI='mongodb+srv://admin:...@cluster.example.mongodb.net/' \
     python3 database/db/apply.py --db cairn --skip-roles        # roles are managed in Atlas (step 2)
   CAIRN_LOADER_MONGODB_URI='mongodb+srv://cairn_loader:...@cluster.example.mongodb.net/' \
     python3 database/tools/load_templates.py --git-release "$(git rev-parse --short HEAD)"
   ```

## Moving an existing PostgreSQL database

If an environment already has data in the PostgreSQL schema (migrations 0001 to 0011), copy it once, with the API and jobs stopped:

```bash
pip install 'psycopg[binary]'
CAIRN_ADMIN_MONGODB_URI=... python3 database/db/apply.py --db cairn            # validators first
CAIRN_POSTGRES_URL=postgresql://owner@host/cairn CAIRN_ADMIN_MONGODB_URI=... \
  python3 database/tools/move_from_postgres.py --db cairn --dry-run           # counts only
CAIRN_POSTGRES_URL=... CAIRN_ADMIN_MONGODB_URI=... python3 database/tools/move_from_postgres.py --db cairn
```

It writes everything in one transaction under the validators, so a row the new schema refuses stops the copy with nothing written. It keeps every id and template version, so tasks stay pinned to what each family was shown. It leaves behind the pre-0008 legal names and plain-text phone numbers on `users`. Keep the PostgreSQL database read-only until the move is checked, then delete it on the retention schedule. [LEGAL REVIEW REQUIRED]

## Development

In GitHub Codespaces the devcontainer runs MongoDB 8.0 with authentication. `make setup` (or `scripts/dev-setup.sh`) initiates the replica set, applies the schema, creates the three users, loads the templates, and writes `.env`. Then:

```bash
make test-db     # every API test on a scratch database, as the cairnApp user
make db-check    # the data security suite only
make db-apply    # after changing db/schema.py
```

The test suite needs an administrator URI for a scratch replica set (`CAIRN_TEST_MONGODB_URI`). It creates and drops its own database, users, and roles. Never point it at real data.

## Changing the schema

Edit `db/schema.py`, then run `db/apply.py`. Validators and indexes are declarative: apply updates validators in place with `collMod` and adds new indexes. It reports, and never drops, an index whose definition changed. A change that needs existing documents rewritten goes in `MIGRATIONS` in `db/apply.py` as a new, numbered function. Never edit one that has run anywhere: apply refuses a changed checksum, like the old SQL migrations.
