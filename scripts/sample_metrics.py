#!/usr/bin/env python3
"""Sample system metrics; append to metric_samples.jsonl and write events to health_events.jsonl.

Runs every 5 minutes via cron. Writes one metric record per run to metric_samples.jsonl.
Writes WARN to health_events.jsonl if chip temp exceeds threshold.
Writes ERROR to health_events.jsonl for any monitored service that is not active.
"""
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from _r2 import load_env

REPO_DIR = Path(__file__).resolve().parent.parent
LOCK_FILE = Path("/tmp/birdnet-sample-metrics.lock")
HEALTH_EVENTS = REPO_DIR / "health_events.jsonl"
METRIC_SAMPLES = REPO_DIR / "metric_samples.jsonl"
TEMP_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
_SERVICES = ["birdnet_analysis", "birdnet_recording", "birdnet_stats", "caddy", "ssh"]
_LOCK_FH = None


def _acquire_lock() -> None:
    global _LOCK_FH
    _LOCK_FH = LOCK_FILE.open("w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"{datetime.now().isoformat()} WARN: Another sample_metrics is already running. Skipping.")
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
    warn_mem_mb = float(os.environ.get("MEMORY_WARN_MB", "100"))
    now = datetime.now(timezone.utc).isoformat()
    sample: dict = {"ts": now}

    # Chip temperature
    try:
        temp_c = int(TEMP_PATH.read_text(encoding="utf-8").strip()) / 1000
        sample["temp_c"] = round(temp_c, 1)
        if temp_c >= warn_c:
            msg = f"{temp_c:.1f}°C (exceeds threshold {warn_c:.0f}°C)"
            _append_event(HEALTH_EVENTS, {"ts": now, "level": "WARN", "source": "temp", "msg": msg})
            print(f"[{datetime.now().isoformat()}] WARN: {msg}")
        else:
            print(f"[{datetime.now().isoformat()}] INFO: temp {temp_c:.1f}°C")
    except OSError as e:
        print(f"ERROR: Cannot read temperature: {e}", file=sys.stderr)

    # Available memory
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                mem_mb = round(int(line.split()[1]) / 1024, 1)
                sample["memory_available_mb"] = mem_mb
                if mem_mb <= warn_mem_mb:
                    msg = f"{mem_mb:.0f} MB available (below threshold {warn_mem_mb:.0f} MB)"
                    _append_event(HEALTH_EVENTS, {"ts": now, "level": "WARN", "source": "memory", "msg": msg})
                    print(f"[{datetime.now().isoformat()}] WARN: {msg}")
                break
    except (OSError, ValueError):
        pass

    # WiFi signal (first wireless interface found)
    try:
        for line in Path("/proc/net/wireless").read_text(encoding="utf-8").splitlines()[2:]:
            parts = line.split()
            if len(parts) >= 4:
                try:
                    sample["wifi_signal_dbm"] = int(float(parts[3].rstrip(".")))
                except ValueError:
                    pass
                break
    except OSError:
        pass

    _append_event(METRIC_SAMPLES, sample)
    print(f"[{datetime.now().isoformat()}] INFO: sample written ({', '.join(f'{k}={v}' for k, v in sample.items() if k != 'ts')})")

    # Service health checks
    try:
        result = subprocess.run(
            ["systemctl", "is-active"] + _SERVICES,
            capture_output=True, text=True,
        )
        statuses = result.stdout.strip().splitlines()
        for svc, status in zip(_SERVICES, statuses):
            if status != "active":
                msg = f"Service {svc} is {status}"
                _append_event(HEALTH_EVENTS, {"ts": now, "level": "ERROR", "source": "services", "msg": msg})
                print(f"[{datetime.now().isoformat()}] ERROR: {msg}")
    except Exception as e:
        print(f"ERROR: Cannot check service status: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
