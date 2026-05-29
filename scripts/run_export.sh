#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="/tmp/birdnet-export.lock"

exec 9>"${LOCK_FILE}"
flock -n 9 || { echo "$(date -Iseconds) WARN: Another export is already running. Skipping."; exit 0; }

if [[ -f "${REPO_DIR}/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${REPO_DIR}/.env"
    set +a
fi

exec python3 "${REPO_DIR}/export_data.py"
