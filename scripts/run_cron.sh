#!/usr/bin/env bash
# Wrapper for cron jobs: logs non-zero exit codes to failures.log.
# Usage: run_cron.sh LABEL COMMAND [ARGS...]

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILURES_LOG="${REPO_DIR}/failures.log"
LOCK_FILE="/tmp/birdnet-failures.lock"

LABEL="${1:?Usage: run_cron.sh LABEL COMMAND [ARGS...]}"
shift

"$@"
rc=$?

if [[ $rc -ne 0 ]]; then
    {
        flock -x 9
        printf '%s ERROR: %s exited with code %d\n' "$(date -Iseconds)" "$LABEL" "$rc" >> "$FAILURES_LOG"
    } 9>"$LOCK_FILE"
fi

exit $rc
