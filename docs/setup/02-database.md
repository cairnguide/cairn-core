# 2. Database (MongoDB Atlas)

Produces: an Atlas cluster with three custom roles, three login users, the schema, and the templates.

Reference: [database/README.md](../../database/README.md). What the collections and roles mean: [Data model](../architecture/data-model.md).

## 2.1 Create the project and cluster

1. In Atlas, create a project per environment (for example `cairn-staging`, `cairn-production`).
2. Create a **dedicated M10 or larger** cluster in the region the data must stay in. Use MongoDB 7.0 or newer (CI tests 7.0 and 8.0).
3. **Encryption at rest** is on by default for every Atlas cluster. Decide whether to add Customer Key Management with your own cloud KMS key **[DECISION NEEDED]**. See [Encryption](../security/encryption.md#at-rest).
4. **Backups:** turn on Cloud Backup with point-in-time restore for production, and practise a restore into a scratch cluster before launch.
5. **Database auditing:** consider turning on Atlas database auditing for authentication failures and role changes (`database/CLAUDE.md`, suggested task 5).

## 2.2 Create the three custom roles

Atlas manages roles and users itself and refuses `createRole` and `createUser` from a driver. Print the role definitions in the Atlas Admin API format:

```bash
python3 database/db/apply.py --print-roles --db cairn
```

Add each one (`cairnApp`, `cairnJobs`, `cairnLoader`) under **Security > Database Access > Custom Roles**, or post the printed bodies to the Admin API. Re-run this after any change to `ROLES` in `database/db/schema.py`.

## 2.3 Create the login users

Create one user per row, each with **only** its one custom role and a long random password from your password manager:

| User | Role | Becomes | Received by |
|---|---|---|---|
| `cairn_api` | `cairnApp` | `MONGODB_URI` | The API container only |
| `cairn_jobs` | `cairnJobs` | `CAIRN_JOBS_MONGODB_URI` | The jobs container only |
| `cairn_loader` | `cairnLoader` | `CAIRN_LOADER_MONGODB_URI` | The deploy workflow only |
| an administrator | Atlas admin | `CAIRN_ADMIN_MONGODB_URI` | The deploy workflow (`apply.py`) only. Never a running service |

Each connection string looks like:

```
mongodb+srv://cairn_api:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority
```

`mongodb+srv` turns TLS on. Don't add `tls=false` or `tlsInsecure=true`.

## 2.4 Network access

Atlas accepts connections only from its IP access list. Cloudflare Containers have no fixed outbound IP addresses, so the list has to admit them broadly (`0.0.0.0/0`), and the protection is TLS plus a strong password per role. **[DECISION NEEDED]** Whether to put a fixed-egress proxy in front before production (`database/CLAUDE.md`, open question 12).

For the GitHub deploy workflow, either the list already admits GitHub's runners, or you give the workflow an Atlas API key with **Project IP Access List Admin** (`ATLAS_PUBLIC_KEY`, `ATLAS_PRIVATE_KEY`, `ATLAS_PROJECT_ID`) and it admits its own IP address for the run, then removes it. See [GitHub](08-github.md).

## 2.5 Apply the schema and load the templates

The [deploy workflow](08-github.md) does this on every deploy. To do it by hand the first time, with the connection strings in your shell only:

```bash
export CAIRN_ADMIN_MONGODB_URI='mongodb+srv://admin:…@cluster.example.mongodb.net/'
```

```bash
.venv/bin/python database/db/apply.py --db cairn --skip-roles
```

```bash
CAIRN_LOADER_MONGODB_URI='mongodb+srv://cairn_loader:…@cluster.example.mongodb.net/' .venv/bin/python database/tools/load_templates.py --git-release "$(git rev-parse --short HEAD)"
```

`--skip-roles` is for Atlas, where roles live in Atlas (2.2). On a self-managed replica set, leave it out and `apply.py` creates the roles, and `database/tools/create_login_user.py` creates the users.

**The template release gate.** The loader refuses templates that haven't had counsel review. The current templates are unreviewed drafts. For staging only, add `--allow-unreviewed`. Production needs that review first **[LEGAL REVIEW REQUIRED]**. The deploy workflow refuses unreviewed templates when the GitHub environment is named `production`.

## 2.6 Moving existing PostgreSQL data

Only if this environment already has data in the old PostgreSQL schema. See [database/README.md](../../database/README.md#moving-an-existing-postgresql-database). Do it now, with the API and jobs stopped.

## Check it

```bash
.venv/bin/python database/db/apply.py --db cairn --skip-roles
```

Running it again should report nothing to change. Keep the four connection strings in your secret manager for [7. Cloudflare](07-cloudflare.md) and [8. GitHub](08-github.md).

Next: [3. Auth0](03-auth0.md).
