#!/usr/bin/env bash
#
# Bring up the stack: generate local secrets, start containers, ensure an admin.
#
# Safe to run repeatedly: setup_env.py only fills missing secrets (never
# rewrites an existing value), the database persists in a named volume, and the
# admin seed skips itself when an admin already exists. The first run with an
# empty volume prompts interactively for the admin credentials.
#
# Options:
#   --build   rebuild images before starting
#   --reset   destroy the database volume and start completely fresh
#             (prompts for confirmation; deletes robots, secrets, users —
#             but keeps .env, so the Fernet key is preserved)
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

usage() {
    echo "Usage: $0 [--build] [--reset]"
    exit 1
}

BUILD=""
RESET=0
for arg in "$@"; do
    case "$arg" in
        --build) BUILD="--build" ;;
        --reset) RESET=1 ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $arg"; usage ;;
    esac
done

if [ "$RESET" -eq 1 ]; then
    read -r -p "Delete ALL database data (robots, secrets, users)? Type 'wipe' to confirm: " ans
    if [ "$ans" != "wipe" ]; then
        echo "Aborted."
        exit 1
    fi
    docker compose down -v
fi

# Generate .env secrets on the HOST, before compose reads them. Idempotent:
# fills only what's missing, never overwrites an existing value (notably the
# Fernet vault key). Must run here, not inside a container — .env has to exist
# before any container can start.
echo "Ensuring .env secrets..."
python3 scripts/setup_env.py

# GITHUB_REPO can't be auto-generated and compose requires it; fail early with a
# clear message instead of an opaque compose substitution error.
if ! grep -q '^GITHUB_REPO=.\+' .env; then
    echo "ERROR: set GITHUB_REPO=owner/repo in .env, then re-run." >&2
    exit 1
fi

echo "Starting stack..."
docker compose up -d ${BUILD} --wait

echo "Ensuring an admin account exists..."
docker compose exec backend python -m opentrons_control.backend.app.scripts.seed_admin

echo "Ready. Proxy on http://localhost:8080"