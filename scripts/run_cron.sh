#!/usr/bin/env bash
# Wrapper for cron jobs: logs non-zero exit codes to failures.log.
# Usage: run_cron.sh LABEL COMMAND [ARGS...]

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILURES_LOG="${REPO_DIR}/failures.log"

LABEL="${1:?Usage: run_cron.sh LABEL COMMAND [ARGS...]}"
shift

"$@"
rc=$?

if [[ $rc -ne 0 ]]; then
    {
        flock -x -w 5 9
        printf '%s ERROR: %s exited with code %d\n' "$(date -Iseconds)" "$LABEL" "$rc" >&9
    } 9>>"$FAILURES_LOG"
fi

exit $rc
