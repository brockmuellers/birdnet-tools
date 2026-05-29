#!/usr/bin/env python3
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

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
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "recent_observations": data["recent"],
        "monthly_stats": data["monthly"],
    }
    try:
        tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp_path, final_path)
    except OSError as e:
        print(f"ERROR: Failed to write JSON: {e}", file=sys.stderr)
        sys.exit(1)


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k = _hmac_sha256(("AWS4" + secret).encode("utf-8"), date_stamp)
    k = _hmac_sha256(k, region)
    k = _hmac_sha256(k, service)
    return _hmac_sha256(k, "aws4_request")


def upload_to_r2(local_path: Path) -> None:
    body = local_path.read_bytes()
    payload_hash = hashlib.sha256(body).hexdigest()

    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    region = "auto"
    service = "s3"

    host = urllib.parse.urlparse(R2_ENDPOINT).netloc
    url = f"{R2_ENDPOINT}/{R2_BUCKET}/{R2_OBJECT_KEY}"
    canonical_uri = f"/{R2_BUCKET}/{urllib.parse.quote(R2_OBJECT_KEY, safe='')}"

    # Headers must be in sorted order for canonical form; host is sent automatically
    # by urllib but must be included here for signing
    canonical_headers = (
        f"cache-control:public, max-age=60\n"
        f"content-type:application/json\n"
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "cache-control;content-type;host;x-amz-content-sha256;x-amz-date"

    canonical_request = "\n".join([
        "PUT",
        canonical_uri,
        "",  # empty query string
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    signing_key = _signing_key(R2_SECRET, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={R2_KEY_ID}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    req = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={
            "Authorization": authorization,
            "Cache-Control": "public, max-age=60",
            "Content-Type": "application/json",
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        },
    )
    try:
        with urllib.request.urlopen(req):
            pass
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        print(f"ERROR: R2 upload failed: HTTP {e.code} {e.reason}", file=sys.stderr)
        if body_text:
            print(body_text, file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"ERROR: R2 upload failed: {e.reason}", file=sys.stderr)
        sys.exit(1)


def main():
    print(f"[{datetime.now().isoformat()}] Starting export...")
    data = query_db(DB_PATH)
    print(f"  Recent observations: {len(data['recent'])}")
    print(f"  Months in stats:     {len(data['monthly'])}")
    write_json(data, TMP_PATH, OUTPUT_PATH)
    print(f"  JSON written to {OUTPUT_PATH}")
    upload_to_r2(OUTPUT_PATH)
    print(f"  Uploaded to R2: s3://{R2_BUCKET}/{R2_OBJECT_KEY}")
    print(f"[{datetime.now().isoformat()}] Done.")


if __name__ == "__main__":
    main()
