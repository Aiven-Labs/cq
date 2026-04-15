#!/bin/sh
set -eu

DB_PATH="${CQ_DB_PATH:-/data/cq.db}"

# Ensure database and auth tables exist before seeding the default user.
/app/.venv/bin/python -c "from pathlib import Path; from cq_server.store import RemoteStore; store = RemoteStore(Path('${DB_PATH}')); store.close()"

/app/.venv/bin/python /app/scripts/seed-users.py --username demo --password demo123 --db "${DB_PATH}"

exec "$@"
