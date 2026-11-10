#!/usr/bin/env python3
"""Sample weather sensors via rtl_433; append one record per run to weather/YYYY-MM.jsonl.

Runs every 5 minutes via cron. Listens on 433.92 MHz for LISTEN_SECONDS and keeps the
latest reading from each sensor. Samples are written to one file per UTC month and are
never pruned: they are the permanent full-resolution archive (backed up nightly to R2 by
backup_db_r2.py).
- LaCrosse-TX141THBv2 thermometer: temp_c, humidity, temp_battery_ok
- Acurite-Rain899 rain gauge:     rain_mm_total (raw counter), rain_mm (delta), rain_battery_ok

rain_mm is the rainfall since the previous rain reading, computed against the last counter
value stored in .weather_state.json. The baseline only advances when the gauge is actually
heard, so rain that falls while the gauge is out of range is counted (in full) on the first
reading after it comes back. A counter decrease means the gauge reset (battery change);
the new counter value is counted as rain since the reset.

If rtl_433 fails, the RTL-SDR dongle is reset with usbreset and rtl_433 is retried once.
Logs WARN when a sensor goes stale (not heard for WEATHER_STALE_WARN_MIN) and when it recovers.

  scripts/sample_weather.py                      # normal cron run
  scripts/sample_weather.py --import FILE        # one-off import of old rtl_433 JSON lines
"""
import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from _r2 import load_env
from _utils import setup_logging

REPO_DIR = Path(__file__).resolve().parent.parent
LOCK_FILE = Path("/tmp/birdnet-sample-weather.lock")
WEATHER_DIR = REPO_DIR / "weather"
STATE_FILE = REPO_DIR / ".weather_state.json"
RTL_USB_ID = "0bda:2838"
LISTEN_SECONDS = 90
TEMP_MODEL = "LaCrosse-TX141THBv2"
RAIN_MODEL = "Acurite-Rain899"
SENSOR_NAMES = {TEMP_MODEL: "thermometer", RAIN_MODEL: "rain gauge"}
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


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _append_samples(samples: list[dict]) -> None:
    """Append samples to weather/YYYY-MM.jsonl, by the UTC month of each sample's ts."""
    by_month: dict[str, list[dict]] = {}
    for s in samples:
        by_month.setdefault(s["ts"][:7], []).append(s)
    WEATHER_DIR.mkdir(exist_ok=True)
    for month, group in by_month.items():
        lines = "".join(json.dumps(s, separators=(",", ":")) + "\n" for s in group)
        with (WEATHER_DIR / f"{month}.jsonl").open("a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(lines)


def parse_readings(lines: list[str], expected_ids: dict[str, int | None]) -> list[dict]:
    """Return rtl_433 records for our sensor models, in input order.

    `expected_ids` maps model -> required id (None accepts any id).
    """
    readings = []
    ignored: set[tuple[str, object]] = set()
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        model = rec.get("model") if isinstance(rec, dict) else None
        if model not in expected_ids:
            continue
        want = expected_ids[model]
        if want is not None and rec.get("id") != want:
            ignored.add((model, rec.get("id")))
            continue
        readings.append(rec)
    for model, sensor_id in sorted(ignored, key=str):
        logging.info("Ignoring %s id %s (expected id %s)", model, sensor_id, expected_ids[model])
    return readings


def rain_delta(prev_total: float | None, total: float) -> float | None:
    """Rainfall since the previous counter reading; None when there is no baseline."""
    if prev_total is None:
        return None
    delta = total - prev_total
    if delta < 0:  # counter reset (e.g. battery change): count everything since the reset
        delta = total
    return round(delta, 3)


def reading_fields(rec: dict) -> dict:
    """Map one rtl_433 record to sample fields (excluding the rain delta)."""
    out: dict = {}
    if rec["model"] == TEMP_MODEL:
        if isinstance(rec.get("temperature_C"), (int, float)):
            out["temp_c"] = round(float(rec["temperature_C"]), 1)
        if isinstance(rec.get("humidity"), (int, float)):
            out["humidity"] = rec["humidity"]
        if "battery_ok" in rec:
            out["temp_battery_ok"] = rec["battery_ok"]
    elif rec["model"] == RAIN_MODEL:
        if isinstance(rec.get("rain_mm"), (int, float)):
            out["rain_mm_total"] = round(float(rec["rain_mm"]), 3)
        if "battery_ok" in rec:
            out["rain_battery_ok"] = rec["battery_ok"]
    return out


def build_sample(ts: str, readings: list[dict], state: dict) -> dict | None:
    """Combine the latest reading per sensor into one sample; advances state's rain baseline."""
    sample: dict = {"ts": ts}
    for rec in readings:  # later readings overwrite earlier ones
        sample.update(reading_fields(rec))
    # The gauge repeats each message; use the most frequent counter value (latest on a tie)
    # so a single corrupt packet can't masquerade as a counter reset.
    totals = [f["rain_mm_total"] for f in map(reading_fields, readings) if "rain_mm_total" in f]
    if totals:
        counts = Counter(totals)
        sample["rain_mm_total"] = max(reversed(totals), key=lambda v: counts[v])
    if "rain_mm_total" in sample:
        delta = rain_delta(state.get("rain_mm_total"), sample["rain_mm_total"])
        if delta is not None:
            sample["rain_mm"] = delta
        state["rain_mm_total"] = sample["rain_mm_total"]
    return sample if len(sample) > 1 else None


def check_staleness(state: dict, heard: set[str], now: datetime, stale_min: float) -> None:
    """Log WARN once when a sensor goes stale and once when it reports again."""
    last_seen = state.setdefault("last_seen", {})
    stale = state.setdefault("stale", {})
    for model, name in SENSOR_NAMES.items():
        if model in heard:
            if stale.get(model) and model in last_seen:
                gap_min = (now - datetime.fromisoformat(last_seen[model])).total_seconds() / 60
                logging.warning("Weather %s reporting again after %d minutes", name, gap_min)
            stale[model] = False
            last_seen[model] = now.isoformat()
            continue
        # Never heard: start the clock now so a sensor that never shows up still warns.
        last_seen.setdefault(model, now.isoformat())
        age_min = (now - datetime.fromisoformat(last_seen[model])).total_seconds() / 60
        if age_min >= stale_min and not stale.get(model):
            logging.warning("No reading from weather %s in %d minutes", name, age_min)
            stale[model] = True


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
            subprocess.run(["usbreset", RTL_USB_ID], capture_output=True, timeout=30)
            time.sleep(2)
    raise RuntimeError(f"rtl_433 {problem} (after usbreset retry)")


def _parse_local_time(t: str) -> datetime:
    """Parse an rtl_433 'time' field (naive local, or with a +HH:MM / +HHMM offset) to UTC."""
    t = t.strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        dt = datetime.strptime(t, "%Y-%m-%dT%H:%M:%S%z")
    return dt.astimezone(timezone.utc)  # naive values are interpreted as local time


def import_history(path: Path, expected_ids: dict[str, int | None], state: dict) -> None:
    """Import old rtl_433 JSON lines (naive local 'time' field) into weather/YYYY-MM.jsonl."""
    readings = parse_readings(path.read_text(encoding="utf-8").splitlines(), expected_ids)
    timed = []
    skipped = 0
    for rec in readings:
        try:
            timed.append((_parse_local_time(str(rec["time"])), rec))
        except (KeyError, ValueError):
            skipped += 1
    seen: set[tuple[datetime, str]] = set()
    samples = []
    prev_total = None
    for dt, rec in sorted(timed, key=lambda x: x[0]):  # stable: file order within a timestamp
        if (dt, rec["model"]) in seen:
            continue
        seen.add((dt, rec["model"]))
        sample = {"ts": dt.isoformat(), **reading_fields(rec)}
        if "rain_mm_total" in sample:
            delta = rain_delta(prev_total, sample["rain_mm_total"])
            if delta is not None:
                sample["rain_mm"] = delta
            prev_total = sample["rain_mm_total"]
        if len(sample) > 1:
            samples.append(sample)
    _append_samples(samples)
    # Let the first live reading count rain since the end of the imported history.
    if prev_total is not None and "rain_mm_total" not in state:
        state["rain_mm_total"] = prev_total
        _save_state(state)
    logging.info("Imported %d samples from %s (%d records skipped: unparseable time)", len(samples), path, skipped)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--import", dest="import_file", type=Path, help="import old rtl_433 JSON lines")
    args = parser.parse_args()

    setup_logging()
    load_env(REPO_DIR / ".env")
    if os.environ.get("WEATHER_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        logging.info("WEATHER_ENABLED is off; skipping")
        return
    _acquire_lock()

    def _id(var: str) -> int | None:
        val = os.environ.get(var, "").strip()
        return int(val) if val else None

    expected_ids = {
        TEMP_MODEL: _id("WEATHER_TEMP_SENSOR_ID"),
        RAIN_MODEL: _id("WEATHER_RAIN_SENSOR_ID"),
    }
    stale_min = float(os.environ.get("WEATHER_STALE_WARN_MIN", "30"))
    state = _load_state()

    if args.import_file:
        import_history(args.import_file, expected_ids, state)
        return

    readings = parse_readings(run_rtl_433(), expected_ids)
    now = datetime.now(timezone.utc)
    sample = build_sample(now.isoformat(), readings, state)
    check_staleness(state, {r["model"] for r in readings}, now, stale_min)
    if sample:
        _append_samples([sample])
        logging.info("sample written (%s)", ", ".join(f"{k}={v}" for k, v in sample.items() if k != "ts"))
    else:
        logging.info("no sensor readings received")
    _save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Unexpected error")
        sys.exit(1)
