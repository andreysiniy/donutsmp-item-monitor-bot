#!/bin/sh
set -eu

alembic upgrade head
exec python -m donutsmp_bot

