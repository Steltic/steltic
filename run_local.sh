#!/usr/bin/env bash
# Local dev server (from a checkout). Loads .env if present, then starts uvicorn.
set -e
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
exec uvicorn steltic.main:app --host 127.0.0.1 --port "${PORT:-8000}" "$@"
