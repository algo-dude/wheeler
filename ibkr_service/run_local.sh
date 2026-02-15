#!/usr/bin/env bash
set -euo pipefail

# Always run from this script's directory (ibkr_service/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found on PATH" >&2
  exit 1
fi

# Load local env file (git-ignored)
if [[ -f ".env.local" ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env.local
  set +a
else
  echo "Missing ibkr_service/.env.local" >&2
  echo "Create it by copying ibkr_service/.env.local.example" >&2
  exit 1
fi

# Auto-derive DB path from Wheeler's active DB pointer if unset.
# Reads the active db name from ../data/currentdb and constructs ../data/<name>.
if [[ -z "${IBKR_DATABASE_PATH:-}" ]]; then
  CURRENTDB_FILE="../data/currentdb"
  if [[ ! -f "$CURRENTDB_FILE" ]]; then
    echo "IBKR_DATABASE_PATH is unset and $CURRENTDB_FILE is missing" >&2
    exit 1
  fi

  ACTIVE_DB_NAME="$(tr -d '[:space:]' < "$CURRENTDB_FILE")"
  if [[ -z "$ACTIVE_DB_NAME" ]]; then
    echo "IBKR_DATABASE_PATH is unset and $CURRENTDB_FILE is empty" >&2
    exit 1
  fi

  export IBKR_DATABASE_PATH="../data/${ACTIVE_DB_NAME}"
  echo "Derived IBKR_DATABASE_PATH=$IBKR_DATABASE_PATH"
fi

# Create venv if missing
if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi

# Activate venv and install deps
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Start the service (reload for local dev)
exec uvicorn main:app \
  --host "${IBKR_SERVICE_HOST:-0.0.0.0}" \
  --port "${IBKR_SERVICE_PORT:-8081}" \
  --reload
