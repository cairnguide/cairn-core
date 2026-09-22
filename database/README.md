# cairn-db-mvp

Database scripts for the Cairn MVP: PostgreSQL migrations, row-level security, a content-as-code template loader, and a security test suite.

Start with [CLAUDE.md](CLAUDE.md) for context, decisions, commands, and open questions.

Quick check (needs a scratch PostgreSQL 15+ server):

```bash
pip install -r tools/requirements.txt
ADMIN_URL=postgresql://admin@localhost/postgres db/tests/run.sh
```
