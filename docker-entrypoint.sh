#!/bin/sh
set -eu

if ! alembic upgrade head; then
    echo >&2 "Database migration failed."
    echo >&2 "If POSTGRES_PASSWORD changed after the PostgreSQL volume was created,"
    echo >&2 "synchronize the existing database role password or restore the original value."
    echo >&2 "See the Database Password Recovery section in README.md."
    exit 1
fi
exec python -m donutsmp_bot
