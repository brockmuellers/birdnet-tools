#!/usr/bin/env python3
"""Export BirdNET-Pi detections to JSON and upload to Cloudflare R2."""
import fcntl
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from _r2 import load_env, upload_to_r2
from _utils import local_timezone_name

REPO_DIR = Path(__file__).resolve().parent.parent
LOCK_FILE = Path("/tmp/birdnet-export.lock")
_LOCK_FH = None


def _acquire_lock() -> None:
    global _LOCK_FH
    _LOCK_FH = LOCK_FILE.open("w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"{datetime.now().isoformat()} WARN: Another export is already running. Skipping.")
        sys.exit(0)


load_env(REPO_DIR / ".env")
_acquire_lock()

DB_PATH = os.environ["BIRDNETPI_DB_PATH"]
R2_ENDPOINT = os.environ["R2_ENDPOINT_URL"]
R2_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET = os.environ["R2_BUCKET"]
R2_OBJECT_KEY = os.environ.get("R2_OBJECT_KEY", "birdnet-data.json")
OUTPUT_PATH = Path(os.environ.get("EXPORT_OUTPUT_PATH", "/tmp/birdnet_export.json"))
TMP_PATH = OUTPUT_PATH.with_suffix(".tmp")

SQL_RECENT = """
    SELECT Com_Name, Confidence, Date || ' ' || Time AS timestamp
    FROM detections
    WHERE Date >= date('now', '-7 days', 'localtime')
    ORDER BY Date DESC, Time DESC;
"""

SQL_MONTHLY = """
    SELECT
        strftime('%Y-%m', Date) AS month,
        Com_Name,
        strftime('%H', Time) || ':' ||
            printf('%02d', (CAST(strftime('%M', Time) AS INTEGER) / 15) * 15) AS bucket,
        COUNT(*) AS count
    FROM detections
    GROUP BY month, Com_Name, bucket
    ORDER BY month, Com_Name, bucket;
"""


def query_db(db_path: str) -> dict:
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as e:
        print(f"ERROR: Cannot open database at {db_path}: {e}", file=sys.stderr)
        sys.exit(1)

    with conn:
        cur = conn.cursor()

        cur.execute(SQL_RECENT)
        recent = [
            {"common_name": row[0], "confidence": row[1], "timestamp": row[2]}
            for row in cur.fetchall()
        ]

        cur.execute(SQL_MONTHLY)
        monthly: dict = {}
        for month, com_name, bucket, count in cur.fetchall():
            monthly.setdefault(month, {}).setdefault(com_name, {})[bucket] = count

    conn.close()
    return {"recent": recent, "monthly": monthly}


def write_json(data: dict, tmp_path: Path, final_path: Path) -> None:
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "timezone": local_timezone_name(),
        "recent_observations": data["recent"],
        "monthly_stats": data["monthly"],
    }
    try:
        tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp_path, final_path)
    except OSError as e:
        print(f"ERROR: Failed to write JSON: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    print(f"[{datetime.now().isoformat()}] Starting export...")
    data = query_db(DB_PATH)
    print(f"  Recent observations: {len(data['recent'])}")
    print(f"  Months in stats:     {len(data['monthly'])}")
    write_json(data, TMP_PATH, OUTPUT_PATH)
    print(f"  JSON written to {OUTPUT_PATH}")
    upload_to_r2(
        OUTPUT_PATH,
        R2_ENDPOINT, R2_KEY_ID, R2_SECRET, R2_BUCKET, R2_OBJECT_KEY,
        content_type="application/json",
        extra_headers={"cache-control": "public, max-age=60"},
    )
    print(f"  Uploaded to R2: s3://{R2_BUCKET}/{R2_OBJECT_KEY}")
    print(f"[{datetime.now().isoformat()}] Done.")


if __name__ == "__main__":
    main()
