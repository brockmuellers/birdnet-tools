#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Installing cron jobs for birdnet-tools at ${REPO_DIR}..."

# Read existing crontab, stripping any previous birdnet-tools entries.
# Catches: REPO= pointing here, absolute-path entries, and $REPO-style entries
# from a previous install of this script.
existing="$(crontab -l 2>/dev/null || true)"
cleaned="$(
    printf '%s\n' "$existing" \
        | grep -vF "REPO=${REPO_DIR}" \
        | grep -vF "${REPO_DIR}/scripts/" \
        | grep -vF '$REPO/scripts/' \
    || true
)"

# \$ produces a literal $ in the crontab so cron expands $REPO at run time.
new_entries="REPO=${REPO_DIR}
*/15 * * * *  \$REPO/scripts/run_cron.sh export       timeout 10m \$REPO/scripts/export_data.py      >> \$REPO/export.log  2>&1
0    2 * * *  \$REPO/scripts/run_cron.sh db-backup     timeout 30m \$REPO/scripts/run_backup.sh --db   >> \$REPO/backup.log  2>&1
0    3 * * 0  \$REPO/scripts/run_cron.sh full-backup   timeout 2h  \$REPO/scripts/run_backup.sh --full  >> \$REPO/backup.log  2>&1
30   2 * * *  \$REPO/scripts/run_cron.sh db-r2-backup  timeout 30m \$REPO/scripts/backup_db_r2.py       >> \$REPO/backup.log  2>&1
*/5  * * * *  \$REPO/scripts/run_cron.sh temp-sample   timeout 30  \$REPO/scripts/sample_temp.py        >> \$REPO/health.log  2>&1
*/15 * * * *  \$REPO/scripts/run_cron.sh push-events   timeout 10m \$REPO/scripts/push_events.py        >> \$REPO/health.log  2>&1"

{
    [[ -n "$cleaned" ]] && printf '%s\n' "$cleaned"
    printf '%s\n' "$new_entries"
} | crontab -

echo "Done. Current crontab:"
crontab -l
