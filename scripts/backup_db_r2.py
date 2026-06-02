#!/usr/bin/env python3
"""Back up birds.db to Cloudflare R2 using SQLite's safe backup API."""
import fcntl
import logging
import os
import sqlite3
import sys
from pathlib import Path

from _r2 import load_env, upload_to_r2
from _utils import setup_logging

REPO_DIR = Path(__file__).resolve().parent.parent
LOCK_FILE = Path("/tmp/birdnet-db-r2-backup.lock")
# /var/tmp persists across reboots and is typically on-disk, not tmpfs —
# avoids doubling DB size in RAM when /tmp is a ramdisk (common on Pi).
TMP_DB = Path("/var/tmp/birdnet-db-r2-backup.db.tmp")

_WARN_PCT = 80
_LOCK_FH = None


def _acquire_lock() -> None:
    global _LOCK_FH
    _LOCK_FH = LOCK_FILE.open("w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logging.warning("Another DB R2 backup is already running. Skipping.")
        sys.exit(0)


setup_logging()
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
    logging.error("R2_DB_BACKUP_MAX_MB must be set in .env")
    sys.exit(1)
try:
    R2_DB_MAX_MB = float(_max_mb_str)
except ValueError:
    logging.error("R2_DB_BACKUP_MAX_MB must be a number, got: %r", _max_mb_str)
    sys.exit(1)


def check_size(db_path: str) -> None:
    size_mb = Path(db_path).stat().st_size / (1024 * 1024)
    warn_threshold_mb = R2_DB_MAX_MB * _WARN_PCT / 100
    if size_mb > R2_DB_MAX_MB:
        logging.error("DB size %.1f MB exceeds max %.0f MB — aborting backup", size_mb, R2_DB_MAX_MB)
        sys.exit(1)
    if size_mb > warn_threshold_mb:
        logging.warning("DB size %.1f MB is >%d%% of max %.0f MB", size_mb, _WARN_PCT, R2_DB_MAX_MB)


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
    logging.info("Starting DB R2 backup...")
    check_size(DB_PATH)
    logging.info("Creating SQLite snapshot...")
    backup_db(DB_PATH, TMP_DB)
    size_mb = TMP_DB.stat().st_size / (1024 * 1024)
    logging.info("Snapshot size: %.1f MB", size_mb)
    try:
        upload_to_r2(
            TMP_DB,
            R2_ENDPOINT, R2_KEY_ID, R2_SECRET, R2_BUCKET, R2_DB_OBJECT_KEY,
            timeout=600,
        )
        logging.info("Uploaded to R2: s3://%s/%s", R2_BUCKET, R2_DB_OBJECT_KEY)
    finally:
        TMP_DB.unlink(missing_ok=True)
    logging.info("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Unexpected error")
        sys.exit(1)
