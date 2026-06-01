"""Shared Cloudflare R2 upload utilities (AWS SigV4 via stdlib urllib)."""
import hashlib
import hmac
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k = _hmac_sha256(("AWS4" + secret).encode("utf-8"), date_stamp)
    k = _hmac_sha256(k, region)
    k = _hmac_sha256(k, service)
    return _hmac_sha256(k, "aws4_request")


def upload_to_r2(
    local_path: Path,
    endpoint: str,
    key_id: str,
    secret: str,
    bucket: str,
    object_key: str,
    content_type: str = "application/octet-stream",
    extra_headers=None,
    timeout: int = 30,
) -> None:
    """Upload local_path to R2 via a signed PUT request.

    extra_headers: dict with lowercase keys added to both signing and the request
    (e.g. {"cache-control": "public, max-age=60"}).
    """
    body = local_path.read_bytes()
    payload_hash = hashlib.sha256(body).hexdigest()

    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    region = "auto"
    service = "s3"

    host = urllib.parse.urlparse(endpoint).netloc
    url = f"{endpoint}/{bucket}/{object_key}"
    canonical_uri = f"/{bucket}/{urllib.parse.quote(object_key, safe='')}"

    headers_to_sign = {
        "content-type": content_type,
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if extra_headers:
        headers_to_sign.update(extra_headers)

    sorted_pairs = sorted(headers_to_sign.items())
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted_pairs)
    signed_headers = ";".join(k for k, _ in sorted_pairs)

    canonical_request = "\n".join([
        "PUT",
        canonical_uri,
        "",
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

    signing_key = _signing_key(secret, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    request_headers = {
        "Authorization": authorization,
        "Content-Type": content_type,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if extra_headers:
        request_headers.update(extra_headers)

    req = urllib.request.Request(url, data=body, method="PUT", headers=request_headers)

    _BACKOFF = [5, 10, 20, 40]  # seconds between attempts 1→2, 2→3, 3→4, 4→5
    for attempt in range(1, 6):
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                return
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            print(f"ERROR: R2 upload failed: HTTP {e.code} {e.reason}", file=sys.stderr)
            if body_text:
                print(body_text, file=sys.stderr)
            sys.exit(1)
        except urllib.error.URLError as e:
            print(f"WARN: R2 upload attempt {attempt}/5 failed: {e.reason}", file=sys.stderr)
            if attempt < 5:
                time.sleep(_BACKOFF[attempt - 1])
    print("ERROR: R2 upload failed after 5 attempts", file=sys.stderr)
    sys.exit(1)
