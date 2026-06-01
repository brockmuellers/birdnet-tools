#!/usr/bin/env python3
"""Sample chip temperature and append to health_events.jsonl."""
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
    level = "WARN" if temp_c >= warn_c else "INFO"
    msg = f"{temp_c:.1f}°C"
    if level == "WARN":
        msg += f" (exceeds threshold {warn_c:.0f}°C)"

    event = {"ts": now, "level": level, "source": "temp", "msg": msg}
    _append_event(HEALTH_EVENTS, event)
    print(f"[{datetime.now().isoformat()}] {level}: {msg}")


if __name__ == "__main__":
    main()
