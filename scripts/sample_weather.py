#!/usr/bin/env python3
"""Sample weather sensors via rtl_433; append one record per run to weather/YYYY-MM.jsonl.

Runs every 5 minutes via cron. Listens on 433.92 MHz for LISTEN_SECONDS and keeps the
latest reading from each sensor. Samples are written to one file per UTC month and are
never pruned: they are the permanent full-resolution archive (backed up nightly to R2 by
backup_db_r2.py).
- LaCrosse-TX141THBv2 thermometer: temp_c, humidity
- Acurite-Rain899 rain gauge:     rain_mm_total (the gauge's raw cumulative counter)

Rainfall per window is derived from consecutive rain_mm_total values by push_events.py.

If rtl_433 fails, the RTL-SDR dongle is reset with usbreset and rtl_433 is retried once."""
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from _r2 import load_env
from _utils import setup_logging

REPO_DIR = Path(__file__).resolve().parent.parent
LOCK_FILE = Path("/tmp/birdnet-sample-weather.lock")
WEATHER_DIR = REPO_DIR / "weather"
RTL_USB_ID = "0bda:2838"
LISTEN_SECONDS = 90
TEMP_MODEL = "LaCrosse-TX141THBv2"
RAIN_MODEL = "Acurite-Rain899"
RTL_CMD = [
    "rtl_433", "-f", "433.92M", "-R", "40", "-R", "73",
    "-T", str(LISTEN_SECONDS), "-F", "json",
]
_LOCK_FH = None


def _acquire_lock() -> None:
    global _LOCK_FH
    _LOCK_FH = LOCK_FILE.open("w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logging.warning("Another sample_weather is already running. Skipping.")
        sys.exit(0)


def _append_sample(sample: dict) -> None:
    """Append sample to weather/YYYY-MM.jsonl for the UTC month of its ts."""
    WEATHER_DIR.mkdir(exist_ok=True)
    with (WEATHER_DIR / f"{sample['ts'][:7]}.jsonl").open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(sample, separators=(",", ":")) + "\n")


def parse_readings(lines: list[str], expected_ids: dict[str, int | None]) -> list[dict]:
    """Return rtl_433 records for our sensor models, in input order.

    `expected_ids` maps model -> required id (None accepts any id).
    """
    readings = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        model = rec.get("model") if isinstance(rec, dict) else None
        if model not in expected_ids:
            continue
        want = expected_ids[model]
        if want is None or rec.get("id") == want:
            readings.append(rec)
    return readings


def reading_fields(rec: dict) -> dict:
    """Map one rtl_433 record to sample fields."""
    out: dict = {}
    if rec["model"] == TEMP_MODEL:
        if isinstance(rec.get("temperature_C"), (int, float)):
            out["temp_c"] = round(float(rec["temperature_C"]), 1)
        if isinstance(rec.get("humidity"), (int, float)):
            out["humidity"] = rec["humidity"]
    elif rec["model"] == RAIN_MODEL:
        if isinstance(rec.get("rain_mm"), (int, float)):
            out["rain_mm_total"] = round(float(rec["rain_mm"]), 3)
    return out


def build_sample(ts: str, readings: list[dict]) -> dict | None:
    """Combine readings into one sample (later readings win); None if nothing usable."""
    sample: dict = {"ts": ts}
    for rec in readings:
        sample.update(reading_fields(rec))
    return sample if len(sample) > 1 else None


def run_rtl_433() -> list[str]:
    """Run rtl_433 once, resetting the dongle and retrying on failure. Returns stdout lines."""
    for attempt in (1, 2):
        try:
            result = subprocess.run(
                RTL_CMD, capture_output=True, text=True, timeout=LISTEN_SECONDS + 30,
            )
            lines = result.stdout.splitlines()
            # Accept captured data even if the exit code is non-zero.
            if result.returncode == 0 or lines:
                return lines
            err = (result.stderr.strip().splitlines() or ["(no stderr)"])[-1]
            problem = f"exited with code {result.returncode}: {err}"
        except subprocess.TimeoutExpired:
            problem = "timed out"
        if attempt == 1:
            logging.info("rtl_433 %s; resetting USB device %s", problem, RTL_USB_ID)
            try:
                subprocess.run(["usbreset", RTL_USB_ID], capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as e:
                logging.info("usbreset failed: %s", e)
            time.sleep(2)
    raise RuntimeError(f"rtl_433 {problem} (after usbreset retry)")


def main():
    setup_logging()
    load_env(REPO_DIR / ".env")
    if os.environ.get("WEATHER_ENABLED") == "0":
        logging.info("WEATHER_ENABLED=0; skipping")
        return
    _acquire_lock()

    def _id(var: str) -> int | None:
        val = os.environ.get(var, "").strip()
        return int(val) if val else None

    expected_ids = {
        TEMP_MODEL: _id("WEATHER_TEMP_SENSOR_ID"),
        RAIN_MODEL: _id("WEATHER_RAIN_SENSOR_ID"),
    }

    readings = parse_readings(run_rtl_433(), expected_ids)
    sample = build_sample(datetime.now(timezone.utc).isoformat(), readings)
    if sample:
        _append_sample(sample)
        logging.info("sample written (%s)", ", ".join(f"{k}={v}" for k, v in sample.items() if k != "ts"))
    else:
        logging.info("no sensor readings received")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Unexpected error")
        sys.exit(1)
