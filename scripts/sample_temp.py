#!/usr/bin/env python3
"""Sample chip temperature; append metric sample to metric_samples.jsonl and WARN events to health_events.jsonl."""
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from _r2 import load_env

REPO_DIR = Path(__file__).resolve().parent.parent
LOCK_FILE = Path("/tmp/birdnet-sample-temp.lock")
HEALTH_EVENTS = REPO_DIR / "health_events.jsonl"
METRIC_SAMPLES = REPO_DIR / "metric_samples.jsonl"
TEMP_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
_LOCK_FH = None


def _acquire_lock() -> None:
    global _LOCK_FH
    _LOCK_FH = LOCK_FILE.open("w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"{datetime.now().isoformat()} WARN: Another temp sample is already running. Skipping.")
        sys.exit(0)


def _append_event(path: Path, event: dict) -> None:
    line = json.dumps(event, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(line)


def main():
    load_env(REPO_DIR / ".env")
    _acquire_lock()

    warn_c = float(os.environ.get("TEMP_WARN_C", "70"))

    try:
        temp_c = int(TEMP_PATH.read_text(encoding="utf-8").strip()) / 1000
    except OSError as e:
        print(f"ERROR: Cannot read temperature: {e}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc).isoformat()

    _append_event(METRIC_SAMPLES, {"ts": now, "temp_c": round(temp_c, 1)})

    if temp_c >= warn_c:
        msg = f"{temp_c:.1f}°C (exceeds threshold {warn_c:.0f}°C)"
        _append_event(HEALTH_EVENTS, {"ts": now, "level": "WARN", "source": "temp", "msg": msg})
        print(f"[{datetime.now().isoformat()}] WARN: {msg}")
    else:
        print(f"[{datetime.now().isoformat()}] INFO: {temp_c:.1f}°C")


if __name__ == "__main__":
    main()
