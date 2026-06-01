#!/usr/bin/env python3
"""Back up birds.db to Cloudflare R2 using SQLite's safe backup API."""
import fcntl
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from _r2 import load_env, upload_to_r2

REPO_DIR = Path(__file__).resolve().parent.parent
LOCK_FILE = Path("/tmp/birdnet-db-r2-backup.lock")
# /var/tmp persists across reboots and is typically on-disk, not tmpfs —
# avoids doubling DB size in RAM when /tmp is a ramdisk (common on Pi).
TMP_DB = Path("/var/tmp/birdnet-db-r2-backup.db.tmp")

_WARN_PCT = 80


def _acquire_lock() -> None:
    lock_fh = LOCK_FILE.open("w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"{datetime.now().isoformat()} WARN: Another DB R2 backup is already running. Skipping.")
        sys.exit(0)
    globals()["_lock_fh"] = lock_fh


load_env(REPO_DIR / ".env")
_acquire_lock()

DB_PATH = os.environ["BIRDNETPI_DB_PATH"]
R2_ENDPOINT = os.environ["R2_ENDPOINT_URL"]
R2_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET = os.environ["R2_BUCKET"]
R2_DB_OBJECT_KEY = os.environ.get("R2_DB_BACKUP_OBJECT_KEY", "birds.db")

_max_mb_str = os.environ.get("R2_DB_BACKUP_MAX_MB", "")
if not _max_mb_str:
    print("ERROR: R2_DB_BACKUP_MAX_MB must be set in .env", file=sys.stderr)
    sys.exit(1)
try:
    R2_DB_MAX_MB = float(_max_mb_str)
except ValueError:
    print(f"ERROR: R2_DB_BACKUP_MAX_MB must be a number, got: {_max_mb_str!r}", file=sys.stderr)
    sys.exit(1)


def check_size(db_path: str) -> None:
    size_mb = Path(db_path).stat().st_size / (1024 * 1024)
    warn_threshold_mb = R2_DB_MAX_MB * _WARN_PCT / 100
    if size_mb > R2_DB_MAX_MB:
        print(
            f"[{datetime.now().isoformat()}] ERROR: DB size {size_mb:.1f} MB exceeds max"
            f" {R2_DB_MAX_MB:.0f} MB — aborting backup",
            file=sys.stderr,
        )
        sys.exit(1)
    if size_mb > warn_threshold_mb:
        print(
            f"[{datetime.now().isoformat()}] WARN: DB size {size_mb:.1f} MB is"
            f" >{_WARN_PCT}% of max {R2_DB_MAX_MB:.0f} MB"
        )


def backup_db(src_path: str, dst_path: Path) -> None:
    dst_path.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dst_path))
    dst.execute("PRAGMA busy_timeout=30000")
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def main():
    print(f"[{datetime.now().isoformat()}] Starting DB R2 backup...")
    check_size(DB_PATH)
    print(f"[{datetime.now().isoformat()}] Creating SQLite snapshot...")
    backup_db(DB_PATH, TMP_DB)
    size_mb = TMP_DB.stat().st_size / (1024 * 1024)
    print(f"  Snapshot size: {size_mb:.1f} MB")
    try:
        upload_to_r2(
            TMP_DB,
            R2_ENDPOINT, R2_KEY_ID, R2_SECRET, R2_BUCKET, R2_DB_OBJECT_KEY,
            timeout=600,
        )
        print(f"  Uploaded to R2: s3://{R2_BUCKET}/{R2_DB_OBJECT_KEY}")
    finally:
        TMP_DB.unlink(missing_ok=True)
    print(f"[{datetime.now().isoformat()}] Done.")


if __name__ == "__main__":
    main()
