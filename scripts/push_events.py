#!/usr/bin/env python3
"""Aggregate BirdNET observation events and system health events, then push to R2.

Sources:
- birdnet_analysis systemd journal: species frequency exclusions (INFO) and errors (ERROR)
- export.log / backup.log: WARN and ERROR lines from cron job scripts
- failures.log: non-zero exit codes captured by run_cron.sh
- health_events.jsonl: WARN/ERROR events from health.log and temp threshold events
- metric_samples.jsonl: periodic numeric samples written by sample_metrics.py

Events are stored locally in birdnet_events.jsonl, health_events.jsonl, and cron_events.jsonl,
pruned to EVENT_LOG_RETAIN_DAYS, merged by timestamp, and uploaded to R2.
Metric samples are bucketed into hourly windows (min/max/avg per key) and uploaded as metric_windows.
"""
import fcntl
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _r2 import load_env, upload_to_r2
from _utils import local_timezone_name

REPO_DIR = Path(__file__).resolve().parent.parent
LOCK_FILE = Path("/tmp/birdnet-push-events.lock")
HEALTH_EVENTS = REPO_DIR / "health_events.jsonl"
BIRDNET_EVENTS = REPO_DIR / "birdnet_events.jsonl"
CRON_EVENTS = REPO_DIR / "cron_events.jsonl"
METRIC_SAMPLES = REPO_DIR / "metric_samples.jsonl"
STATE_FILE = REPO_DIR / ".health_state.json"
_LOCK_FH = None

EXCLUSION_MARKER = "Excluded as below Species Occurrence Frequency Threshold: "

# Matches bracketed timestamps [2026-06-01T12:34:56...] or [2026-06-01 12:34:56]
# and bare ISO timestamps 2026-06-01T12:34:56... at the start of a line.
_TS_RE = re.compile(
    r"^(?:\[(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\]]*)\]"
    r"|(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s]*))"
)
# Matches WARN: or ERROR: anywhere in a line (word-boundary anchored to avoid
# matching substrings like "ValueError:").
_LEVEL_RE = re.compile(r"\b(WARN|ERROR):\s*(.*)")


def _acquire_lock() -> None:
    global _LOCK_FH
    _LOCK_FH = LOCK_FILE.open("w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"{datetime.now().isoformat()} WARN: Another push_events is already running. Skipping.")
        sys.exit(0)


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _append_events(path: Path, events: list[dict]) -> None:
    if not events:
        return
    lines = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events)
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(lines)


def _prune_events(path: Path, retain_days: int) -> None:
    """Rewrite path in-place, dropping events older than retain_days.

    Uses in-place truncation (rather than rename) so that sample_metrics.py's
    concurrent appends always land on the same inode.
    """
    if not path.exists():
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).isoformat()
    with path.open("r+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        lines = fh.read().splitlines()
        kept = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                if json.loads(line).get("ts", "") >= cutoff:
                    kept.append(line)
            except json.JSONDecodeError:
                pass
        fh.seek(0)
        fh.write("".join(l + "\n" for l in kept))
        fh.truncate()


def _load_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


def _parse_ts(ts_str: str) -> str:
    ts_str = ts_str.strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return datetime.now(timezone.utc).isoformat()


def collect_journal_events(state: dict, retain_days: int) -> tuple[list[dict], str | None]:
    """Collect new birdnet_analysis journal events since the last cursor."""
    cursor = state.get("journal_cursor")
    cmd = ["journalctl", "-u", "birdnet_analysis", "--output=json", "--no-pager"]
    if cursor:
        cmd += ["--after-cursor", cursor]
    else:
        since = (datetime.now() - timedelta(days=retain_days)).strftime("%Y-%m-%d %H:%M:%S")
        cmd += ["--since", since]

    result = subprocess.run(cmd, capture_output=True, text=True)

    events: list[dict] = []
    new_cursor: str | None = None
    for line in result.stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        new_cursor = obj.get("__CURSOR", new_cursor)
        if "__REALTIME_TIMESTAMP" not in obj:
            continue
        msg = obj.get("MESSAGE", "")
        if not isinstance(msg, str):
            continue
        ts = datetime.fromtimestamp(
            int(obj["__REALTIME_TIMESTAMP"]) / 1e6, tz=timezone.utc
        ).isoformat()

        if EXCLUSION_MARKER in msg:
            # Message format: "[utils.analysis][WARNING] Excluded as below ... Threshold: Sci Name Com Name"
            idx = msg.index(EXCLUSION_MARKER) + len(EXCLUSION_MARKER)
            words = msg[idx:].strip().split()
            if len(words) >= 3:
                sci_name = " ".join(words[:2])
                com_name = " ".join(words[2:])
                event_msg = f"Excluded: {com_name} ({sci_name})"
            else:
                event_msg = f"Excluded: {msg[idx:].strip()}"
            events.append({"ts": ts, "level": "INFO", "source": "birdnet", "msg": event_msg})
        elif "][ERROR]" in msg:
            # BirdNET-Pi uses Python logging with format "[module][LEVEL] message"
            events.append({"ts": ts, "level": "ERROR", "source": "birdnet", "msg": msg})

    return events, new_cursor


def collect_log_events(log_path: Path, source: str, offset: int) -> tuple[list[dict], int]:
    """Collect WARN/ERROR lines from a log file since the given byte offset."""
    if not log_path.exists():
        return [], offset

    with log_path.open("rb") as fh:
        fh.seek(offset)
        new_data = fh.read()
        new_offset = fh.tell()

    events: list[dict] = []
    last_ts = datetime.now(timezone.utc).isoformat()
    for raw_line in new_data.decode("utf-8", errors="replace").splitlines():
        # Update last_ts from any timestamped line, not just WARN/ERROR ones,
        # so that untimestamped error lines inherit an accurate timestamp.
        ts_m = _TS_RE.match(raw_line)
        if ts_m:
            last_ts = _parse_ts(ts_m.group(1) or ts_m.group(2))

        level_m = _LEVEL_RE.search(raw_line)
        if not level_m:
            continue

        level = level_m.group(1)
        msg = level_m.group(2).strip()
        events.append({"ts": last_ts, "level": level, "source": source, "msg": msg})

    return events, new_offset


def collect_health_snapshot(db_path: str | None, backup_dest: str | None, last_upload_at: str | None, latest_sample: dict | None = None) -> dict:
    snapshot: dict = {}
    if latest_sample is None:
        latest_sample = {}

    if last_upload_at is not None:
        snapshot["last_successful_upload_at"] = last_upload_at

    # Disk usage: always include /, add BACKUP_DEST if it's a different filesystem
    def _disk_stat(path: str) -> dict:
        st = os.statvfs(path)
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        used_pct = round(used / (used + avail) * 100, 1) if (used + avail) else 0
        return {"used_pct": used_pct, "free_gb": round(avail / 1024 ** 3, 2)}

    disks: dict = {}
    try:
        disks["/"] = _disk_stat("/")
    except OSError:
        pass
    if backup_dest:
        try:
            if os.stat(backup_dest).st_dev != os.stat("/").st_dev:
                disks[backup_dest] = _disk_stat(backup_dest)
        except OSError:
            pass
    if disks:
        snapshot["disk"] = disks

    # Last detection timestamp and DB size
    if db_path and Path(db_path).exists():
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
                row = conn.execute(
                    "SELECT Date || 'T' || Time FROM detections ORDER BY Date DESC, Time DESC LIMIT 1"
                ).fetchone()
            snapshot["last_detection_at"] = row[0] if row else None
            snapshot["db_size_mb"] = round(Path(db_path).stat().st_size / 1024 ** 2, 2)
        except Exception:
            pass

    # System uptime
    try:
        snapshot["uptime_seconds"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except OSError:
        pass

    # Metrics sourced from latest sample collected by sample_metrics.py
    for key in ("temp_c", "memory_available_mb", "wifi_signal_dbm"):
        if key in latest_sample:
            snapshot[key] = latest_sample[key]

    # Network: interface states + primary outbound IP
    interfaces: dict = {}
    net_root = Path("/sys/class/net")
    if net_root.exists():
        for iface_path in sorted(net_root.iterdir()):
            name = iface_path.name
            if name == "lo":
                continue
            if "/virtual/" in str(iface_path.resolve()):
                continue
            try:
                state = (iface_path / "operstate").read_text().strip()
                interfaces[name] = {"state": state}
            except OSError:
                pass

    if interfaces:
        snapshot["interfaces"] = interfaces

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        snapshot["primary_ip"] = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    return snapshot


def aggregate_metric_windows(samples: list[dict]) -> list[dict]:
    """Bucket metric samples into hourly windows, computing min/max/avg per numeric key."""
    buckets: dict[str, dict[str, list[float]]] = {}
    for sample in samples:
        ts = sample.get("ts", "")
        if len(ts) < 13:
            continue
        window = ts[:13] + ":00:00" + ts[19:]  # truncate to hour
        bucket = buckets.setdefault(window, {})
        for key, val in sample.items():
            if key == "ts" or not isinstance(val, (int, float)):
                continue
            bucket.setdefault(key, []).append(float(val))

    windows = []
    for window_start, metrics in sorted(buckets.items()):
        entry: dict = {"window_start": window_start}
        for key, vals in sorted(metrics.items()):
            entry[key] = {
                "min": round(min(vals), 2),
                "max": round(max(vals), 2),
                "avg": round(sum(vals) / len(vals), 2),
            }
        windows.append(entry)
    return windows


def main():
    load_env(REPO_DIR / ".env")
    _acquire_lock()

    R2_ENDPOINT = os.environ["R2_ENDPOINT_URL"]
    R2_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
    R2_SECRET = os.environ["R2_SECRET_ACCESS_KEY"]
    R2_BUCKET = os.environ["R2_BUCKET"]
    EVENT_LOG_KEY = os.environ.get("EVENT_LOG_OBJECT_KEY", "birdnet-events.json")
    RETAIN_DAYS = int(os.environ.get("EVENT_LOG_RETAIN_DAYS", "7"))
    DB_PATH = os.environ.get("BIRDNETPI_DB_PATH")
    BACKUP_DEST = os.environ.get("BACKUP_DEST")

    state = _load_state()

    print(f"[{datetime.now().isoformat()}] Collecting journal events...")
    journal_events, new_cursor = collect_journal_events(state, RETAIN_DAYS)
    if journal_events:
        _append_events(BIRDNET_EVENTS, journal_events)
        print(f"  {len(journal_events)} new birdnet event(s)")
    if new_cursor:
        state["journal_cursor"] = new_cursor

    print(f"[{datetime.now().isoformat()}] Collecting log file events...")
    for log_filename, source, state_key, dest in [
        ("export.log",   "export",  "export_log_offset",   CRON_EVENTS),
        ("backup.log",   "backup",  "backup_log_offset",   CRON_EVENTS),
        ("health.log",   "health",  "health_log_offset",   HEALTH_EVENTS),
        ("failures.log", "cron",    "failures_log_offset", CRON_EVENTS),
    ]:
        offset = state.get(state_key, 0)
        events, new_offset = collect_log_events(REPO_DIR / log_filename, source, offset)
        if events:
            _append_events(dest, events)
            print(f"  {len(events)} new {source} log event(s)")
        state[state_key] = new_offset

    _save_state(state)

    print(f"[{datetime.now().isoformat()}] Pruning old events...")
    _prune_events(HEALTH_EVENTS, RETAIN_DAYS)
    _prune_events(BIRDNET_EVENTS, RETAIN_DAYS)
    _prune_events(CRON_EVENTS, RETAIN_DAYS)
    _prune_events(METRIC_SAMPLES, RETAIN_DAYS)

    print(f"[{datetime.now().isoformat()}] Collecting health snapshot...")
    metric_samples = _load_events(METRIC_SAMPLES)
    latest_sample = max(metric_samples, key=lambda s: s.get("ts", ""), default={})
    health_snapshot = collect_health_snapshot(DB_PATH, BACKUP_DEST, state.get("last_successful_upload_at"), latest_sample)

    print(f"[{datetime.now().isoformat()}] Merging and uploading...")
    health = _load_events(HEALTH_EVENTS)
    birdnet = _load_events(BIRDNET_EVENTS)
    cron = _load_events(CRON_EVENTS)
    all_events = sorted(health + birdnet + cron, key=lambda e: e.get("ts", ""), reverse=True)

    metric_windows = aggregate_metric_windows(metric_samples)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": local_timezone_name(),
        "health": health_snapshot,
        "events": all_events,
        "metric_windows": metric_windows,
    }

    fd, tmp_path_str = tempfile.mkstemp(suffix=".json", prefix="birdnet-events-")
    tmp_path = Path(tmp_path_str)
    try:
        os.close(fd)
        tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        upload_to_r2(
            tmp_path,
            R2_ENDPOINT, R2_KEY_ID, R2_SECRET, R2_BUCKET, EVENT_LOG_KEY,
            content_type="application/json",
            extra_headers={"cache-control": "public, max-age=60"},
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    state["last_successful_upload_at"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    print(f"  Uploaded to R2: s3://{R2_BUCKET}/{EVENT_LOG_KEY}")
    print(f"  Total events: {len(all_events)} ({len(health)} health, {len(birdnet)} birdnet, {len(cron)} cron)")
    print(f"  Metric windows: {len(metric_windows)} ({len(metric_samples)} samples)")
    print(f"[{datetime.now().isoformat()}] Done.")


if __name__ == "__main__":
    main()
