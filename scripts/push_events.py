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
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _r2 import load_env, upload_to_r2
from _utils import local_timezone_name, setup_logging

REPO_DIR = Path(__file__).resolve().parent.parent
LOCK_FILE = Path("/tmp/birdnet-push-events.lock")
HEALTH_EVENTS = REPO_DIR / "health_events.jsonl"
BIRDNET_EVENTS = REPO_DIR / "birdnet_events.jsonl"
CRON_EVENTS = REPO_DIR / "cron_events.jsonl"
METRIC_SAMPLES = REPO_DIR / "metric_samples.jsonl"
STATE_FILE = REPO_DIR / ".health_state.json"
SPECIES_FREQ_CACHE = REPO_DIR / ".species_freq_cache.json"
_LOCK_FH = None

EXCLUSION_MARKER = "Excluded as below Species Occurrence Frequency Threshold: "
# Non-bird sounds aggregated into a single count event rather than individual exclusion events
NON_BIRD_SPECIES = ["Fireworks", "Engine", "Dog", "Siren"]

# Matches bracketed timestamps [2026-06-01T12:34:56...] or [2026-06-01 12:34:56]
# and bare ISO timestamps 2026-06-01T12:34:56... at the start of a line.
_TS_RE = re.compile(
    r"^(?:\[(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\]]*)\]"
    r"|(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s]*))"
)
# Matches WARN: or ERROR: anywhere in a line (word-boundary anchored to avoid
# matching substrings like "ValueError:").
_LEVEL_RE = re.compile(r"\b(WARN|ERROR):\s*(.*)")


def _load_nonzero_species() -> frozenset[str] | None:
    """Load the set of frequency-excluded species from the weekly cache.

    Returns the names of species present in this region but below SF_THRESH —
    the band that BirdNET-Pi excludes and that we want to surface as events.
    Returns None (fail-open) if the cache is absent or unreadable, so that
    all exclusions are recorded rather than silently dropped.
    """
    if not SPECIES_FREQ_CACHE.exists():
        logging.warning(
            "Species frequency cache not found (%s); all exclusions will be recorded. "
            "Run refresh_species_freq.py to generate it.",
            SPECIES_FREQ_CACHE,
        )
        return None
    try:
        data = json.loads(SPECIES_FREQ_CACHE.read_text(encoding="utf-8"))
        return frozenset(data["species_frequencies"].keys())
    except (json.JSONDecodeError, KeyError, OSError) as exc:
        logging.warning(
            "Could not read species frequency cache (%s); all exclusions will be recorded",
            exc,
        )
        return None


def _acquire_lock() -> None:
    global _LOCK_FH
    _LOCK_FH = LOCK_FILE.open("w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logging.warning("Another push_events is already running. Skipping.")
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


def collect_journal_events(
    state: dict,
    retain_days: int,
    nonzero_species: frozenset[str] | None = None,
) -> tuple[list[dict], str | None]:
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
    non_bird_counts: dict[str, int] = {name: 0 for name in NON_BIRD_SPECIES}
    window_start: str | None = None
    window_end: str | None = None
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
        if window_start is None:
            window_start = ts
        window_end = ts

        if EXCLUSION_MARKER in msg:
            # Message format: "[utils.analysis][WARNING] Excluded as below ... Threshold: Sci Name Com Name"
            idx = msg.index(EXCLUSION_MARKER) + len(EXCLUSION_MARKER)
            words = msg[idx:].strip().split()
            if len(words) >= 3:
                sci_name = " ".join(words[:2])
                com_name = " ".join(words[2:])
                if com_name in non_bird_counts:
                    non_bird_counts[com_name] += 1
                    continue
                if nonzero_species is not None and f"{sci_name}_{com_name}" not in nonzero_species:
                    continue
                event_msg = f"Excluded: {com_name} ({sci_name})"
            else:
                # Non-bird sounds have a 1-word sci name == com name (e.g. "Fireworks Fireworks")
                com_name = words[-1] if words else ""
                if com_name in non_bird_counts:
                    non_bird_counts[com_name] += 1
                    continue
                event_msg = f"Excluded: {msg[idx:].strip()}"
            events.append({"ts": ts, "level": "INFO", "source": "birdnet", "msg": event_msg})
        elif "][ERROR]" in msg:
            # BirdNET-Pi uses Python logging with format "[module][LEVEL] message"
            events.append({"ts": ts, "level": "ERROR", "source": "birdnet", "msg": msg})

    if any(c > 0 for c in non_bird_counts.values()):
        parts = ", ".join(f"{non_bird_counts[n]} {n}" for n in NON_BIRD_SPECIES)
        if window_start and window_end:
            fmt = "%H:%M"
            t0 = datetime.fromisoformat(window_start).strftime(fmt)
            t1 = datetime.fromisoformat(window_end).strftime(fmt)
            window_str = f" ({t0}–{t1} UTC)"
        else:
            window_str = ""
        events.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": "INFO",
            "source": "birdnet",
            "msg": f"Detected: {parts}{window_str}",
        })

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

        if not ts_m:
            logging.warning(
                "Log line from source %r had no timestamp; assigned %s: %s",
                source, last_ts, raw_line.strip(),
            )

        level = level_m.group(1)
        msg = level_m.group(2).strip()
        events.append({"ts": last_ts, "level": level, "source": source, "msg": msg})

    return events, new_offset


def check_ip_change(state: dict) -> list[dict]:
    """Return a WARN event if primary_ip changed since the last run, and update state."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        current_ip = s.getsockname()[0]
        s.close()
    except Exception:
        return []

    events = []
    last_ip = state.get("last_primary_ip")
    if last_ip and last_ip != current_ip:
        msg = f"primary_ip changed from {last_ip} to {current_ip} — mDNS hostname (.local) may no longer resolve correctly"
        events.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": "WARN",
            "source": "network",
            "msg": msg,
        })
    state["last_primary_ip"] = current_ip
    return events


def collect_health_snapshot(db_path: str | None, last_upload_at: str | None, latest_sample: dict | None = None) -> dict:
    snapshot: dict = {}
    if latest_sample is None:
        latest_sample = {}

    if last_upload_at is not None:
        snapshot["last_successful_upload_at"] = last_upload_at

    # Disk usage from latest metric sample written by sample_metrics.py
    disks: dict = {}
    if "disk_root_used_pct" in latest_sample:
        disks["/"] = {"used_pct": latest_sample["disk_root_used_pct"], "free_gb": latest_sample.get("disk_root_free_gb")}
    if "disk_backup_used_pct" in latest_sample:
        backup_dest = os.environ.get("BACKUP_DEST", "backup")
        disks[backup_dest] = {"used_pct": latest_sample["disk_backup_used_pct"], "free_gb": latest_sample.get("disk_backup_free_gb")}
    if disks:
        snapshot["disk"] = disks

    # DB size
    if db_path and Path(db_path).exists():
        try:
            snapshot["db_size_mb"] = round(Path(db_path).stat().st_size / 1024 ** 2, 2)
        except OSError:
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

    try:
        snapshot["hostname"] = socket.gethostname()
    except Exception:
        pass

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        snapshot["primary_ip"] = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    return snapshot


def summarize_metric_period(samples: list[dict], hours: int) -> dict:
    """Aggregate all numeric metric keys across samples within the last `hours` hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    buckets: dict[str, list[float]] = {}
    for sample in samples:
        if sample.get("ts", "") < cutoff:
            continue
        for key, val in sample.items():
            if key == "ts" or not isinstance(val, (int, float)):
                continue
            buckets.setdefault(key, []).append(float(val))
    return {
        key: {
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "avg": round(sum(vals) / len(vals), 2),
        }
        for key, vals in sorted(buckets.items())
    }


def aggregate_metric_windows(samples: list[dict]) -> list[dict]:
    """Bucket metric samples into hourly windows, computing min/max/avg per numeric key."""
    buckets: dict[str, dict[str, list[float]]] = {}
    for sample in samples:
        ts = sample.get("ts", "")
        if len(ts) < 13:
            continue
        window = ts[:13] + ":00:00+00:00"  # truncate to hour
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
    setup_logging()
    load_env(REPO_DIR / ".env")
    _acquire_lock()

    R2_ENDPOINT = os.environ["R2_ENDPOINT_URL"]
    R2_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
    R2_SECRET = os.environ["R2_SECRET_ACCESS_KEY"]
    R2_BUCKET = os.environ["R2_BUCKET"]
    EVENT_LOG_KEY = os.environ.get("EVENT_LOG_OBJECT_KEY", "birdnet-events.json")
    RETAIN_DAYS = int(os.environ.get("EVENT_LOG_RETAIN_DAYS", "7"))
    DB_PATH = os.environ.get("BIRDNETPI_DB_PATH")

    state = _load_state()

    ip_events = check_ip_change(state)
    if ip_events:
        _append_events(HEALTH_EVENTS, ip_events)
        logging.info("%d IP change event(s): %s", len(ip_events), ip_events[0]["msg"])

    nonzero_species = _load_nonzero_species()

    logging.info("Collecting journal events...")
    journal_events, new_cursor = collect_journal_events(state, RETAIN_DAYS, nonzero_species)
    if journal_events:
        _append_events(BIRDNET_EVENTS, journal_events)
        logging.info("%d new birdnet event(s)", len(journal_events))
    if new_cursor:
        state["journal_cursor"] = new_cursor

    logging.info("Collecting log file events...")
    for log_filename, source, state_key, dest in [
        ("logs/export.log",   "export",  "export_log_offset",   CRON_EVENTS),
        ("logs/backup.log",   "backup",  "backup_log_offset",   CRON_EVENTS),
        ("logs/health.log",   "health",  "health_log_offset",   HEALTH_EVENTS),
        ("logs/failures.log", "cron",    "failures_log_offset", CRON_EVENTS),
    ]:
        offset = state.get(state_key, 0)
        events, new_offset = collect_log_events(REPO_DIR / log_filename, source, offset)
        if events:
            _append_events(dest, events)
            logging.info("%d new %s log event(s)", len(events), source)
        state[state_key] = new_offset

    _save_state(state)

    logging.info("Pruning old events...")
    _prune_events(HEALTH_EVENTS, RETAIN_DAYS)
    _prune_events(BIRDNET_EVENTS, RETAIN_DAYS)
    _prune_events(CRON_EVENTS, RETAIN_DAYS)
    _prune_events(METRIC_SAMPLES, RETAIN_DAYS)

    logging.info("Collecting health snapshot...")
    metric_samples = _load_events(METRIC_SAMPLES)
    latest_sample = max(metric_samples, key=lambda s: s.get("ts", ""), default={})
    health_snapshot = collect_health_snapshot(DB_PATH, state.get("last_successful_upload_at"), latest_sample)

    logging.info("Merging and uploading...")
    health = _load_events(HEALTH_EVENTS)
    birdnet = _load_events(BIRDNET_EVENTS)
    cron = _load_events(CRON_EVENTS)
    all_events = sorted(health + birdnet + cron, key=lambda e: e.get("ts", ""), reverse=True)

    _MAX_EVENTS = 5000
    if len(all_events) > _MAX_EVENTS:
        total = len(all_events)
        all_events = all_events[:_MAX_EVENTS]
        omitted = total - _MAX_EVENTS
        all_events.append({
            "ts": all_events[-1].get("ts", datetime.now(timezone.utc).isoformat()),
            "level": "ERROR",
            "source": "truncation",
            "msg": f"Showing {_MAX_EVENTS} most recent events; {omitted} older event(s) not included",
        })

    metric_windows = aggregate_metric_windows(metric_samples)
    last_hour = summarize_metric_period(metric_samples, 1)
    last_day = summarize_metric_period(metric_samples, 24)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": local_timezone_name(),
        "health": health_snapshot,
        "events": all_events,
        "metric_windows": metric_windows,
        "last_hour": last_hour,
        "last_day": last_day,
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

    logging.info("Uploaded to R2: s3://%s/%s", R2_BUCKET, EVENT_LOG_KEY)
    logging.info("Total events: %d (%d health, %d birdnet, %d cron)", len(all_events), len(health), len(birdnet), len(cron))
    logging.info("Metric windows: %d (%d samples); last_hour: %d keys, last_day: %d keys", len(metric_windows), len(metric_samples), len(last_hour), len(last_day))
    logging.info("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Unexpected error")
        sys.exit(1)
