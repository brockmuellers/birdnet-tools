#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="/tmp/birdnet-backup.lock"

exec 9>"${LOCK_FILE}"
flock -n 9 || { echo "$(date -Iseconds) WARN: Another backup is already running. Skipping."; exit 0; }

if [[ -f "${REPO_DIR}/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${REPO_DIR}/.env"
    set +a
fi

BIRDNETPI_DB_PATH="${BIRDNETPI_DB_PATH:?BIRDNETPI_DB_PATH must be set in .env}"
BACKUP_DEST="${BACKUP_DEST:?BACKUP_DEST must be set in .env}"
BACKUP_DB_RETAIN_DAYS="${BACKUP_DB_RETAIN_DAYS:-7}"
BACKUP_DISK_WARN_PCT="${BACKUP_DISK_WARN_PCT:-80}"
BIRDNET_BACKUP_SCRIPT="${HOME}/BirdNET-Pi/scripts/backup_data.sh"

MODE="${1:-}"

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*"
}

trap 'log "ERROR: command failed at line $LINENO: $BASH_COMMAND"' ERR

check_disk() {
    local used_pct
    used_pct=$(df --output=pcent "$BACKUP_DEST" | tail -1 | tr -d ' %')
    if [[ "$used_pct" -ge "$BACKUP_DISK_WARN_PCT" ]]; then
        log "WARN: Backup disk ${used_pct}% full (threshold: ${BACKUP_DISK_WARN_PCT}%)"
    fi
}

do_full() {
    local final="${BACKUP_DEST}/birdnet-full-backup.tar"
    local tmp="${final}.tmp"
    rm -f "$tmp"
    log "Starting full backup to ${final} (BirdNET services will pause during backup)..."
    set +e
    "$BIRDNET_BACKUP_SCRIPT" -a backup -f "$tmp"
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        rm -f "$tmp"
        log "ERROR: Full backup failed (exit code ${rc})"
        exit 1
    fi
    mv -f "$tmp" "$final"
    log "Full backup complete: $(du -sh "$final" | cut -f1)"
    check_disk
}

do_db() {
    local date_str
    date_str=$(date '+%Y-%m-%d')
    local db_dir="${BACKUP_DEST}/birds-db"
    local dest="${db_dir}/birds-${date_str}.db"
    local tmp="${dest}.tmp"
    mkdir -p "$db_dir"
    log "Backing up DB to ${dest}..."
    set +e
    python3 - "$BIRDNETPI_DB_PATH" "$tmp" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
dst.execute("PRAGMA busy_timeout=30000")
src.backup(dst)
src.close()
dst.close()
EOF
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        rm -f "$tmp"
        log "ERROR: DB backup failed (exit code ${rc})"
        exit 1
    fi
    mv -f "$tmp" "$dest"
    # Prune: keep the last BACKUP_DB_RETAIN_DAYS backups
    local to_delete
    to_delete=$(find "$db_dir" -name "birds-*.db" -type f | sort | head -n "-${BACKUP_DB_RETAIN_DAYS}")
    if [[ -n "$to_delete" ]]; then
        while IFS= read -r f; do
            log "Removing old DB backup: $(basename "$f")"
            rm "$f"
        done <<< "$to_delete"
    fi
    log "DB backup complete"
    check_disk
}

db_dev=$(stat --format="%d" "$BIRDNETPI_DB_PATH" 2>/dev/null || stat --format="%d" "$(dirname "$BIRDNETPI_DB_PATH")")
dest_dev=$(stat --format="%d" "$BACKUP_DEST" 2>/dev/null || stat --format="%d" "$(dirname "$BACKUP_DEST")")
if [[ "$db_dev" == "$dest_dev" ]]; then
    log "WARN: BACKUP_DEST (${BACKUP_DEST}) is on the same drive as the source DB (${BIRDNETPI_DB_PATH}). A drive failure will lose both the source and the backup."
fi

case "$MODE" in
    --full) do_full ;;
    --db)   do_db ;;
    *)      echo "Usage: $0 --full | --db" >&2; exit 1 ;;
esac
