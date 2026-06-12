#!/usr/bin/env python3
"""Sample system metrics; append to metric_samples.jsonl and write events to health_events.jsonl.

Runs every 5 minutes via cron. Writes one metric record per run to metric_samples.jsonl.
Writes WARN to health_events.jsonl if chip temp exceeds threshold.
Writes ERROR to health_events.jsonl for any monitored service that is not active.
"""
import fcntl
import json
import logging
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from _r2 import load_env
from _utils import setup_logging

REPO_DIR = Path(__file__).resolve().parent.parent
LOCK_FILE = Path("/tmp/birdnet-sample-metrics.lock")
HEALTH_EVENTS = REPO_DIR / "health_events.jsonl"
METRIC_SAMPLES = REPO_DIR / "metric_samples.jsonl"
METRICS_STATE_FILE = REPO_DIR / ".sample_metrics_state.json"
HEALTH_STATE_FILE = REPO_DIR / ".health_state.json"
TEMP_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
_SYSTEMCTL_SERVICES = ["birdnet_analysis", "birdnet_recording", "birdnet_stats"]
_TCP_SERVICES = {"ssh": ("127.0.0.1", 22), "caddy": ("127.0.0.1", 80)}
_LOCK_FH = None


def _root_block_device() -> str | None:
    """Return the kernel device name (e.g. mmcblk0, sda) backing the root filesystem."""
    try:
        for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[1] != "/" or not parts[0].startswith("/dev/"):
                continue
            name = parts[0][5:]  # strip /dev/
            # mmcblkNpM, nvmeNnXpM -> strip pM to get device name
            stripped = re.sub(r"p\d+$", "", name)
            if stripped != name:
                return stripped
            # sdXN, vdXN -> strip trailing partition number
            return re.sub(r"\d+$", "", name) or name
    except OSError:
        pass
    return None


def _read_diskstats(device: str) -> tuple[int, int] | None:
    """Return (sectors_read, sectors_written) for `device` from /proc/diskstats."""
    try:
        for line in Path("/proc/diskstats").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 10 and parts[2] == device:
                return int(parts[5]), int(parts[9])
    except OSError:
        pass
    return None


def _tcp_check(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _default_gateway() -> str | None:
    """Return the IPv4 default gateway from /proc/net/route, or None if not found."""
    try:
        for line in Path("/proc/net/route").read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "00000000" and parts[2] != "00000000":
                gw_hex = parts[2]
                if len(gw_hex) == 8:
                    return ".".join(str(b) for b in bytes.fromhex(gw_hex)[::-1])
    except OSError:
        pass
    return None


def _ping(host: str, count: int = 2, timeout: int = 2) -> bool:
    result = subprocess.run(
        ["ping", "-n", "-c", str(count), "-W", str(timeout), host],
        capture_output=True,
    )
    return result.returncode == 0


def _acquire_lock() -> None:
    global _LOCK_FH
    _LOCK_FH = LOCK_FILE.open("w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logging.warning("Another sample_metrics is already running. Skipping.")
        sys.exit(0)


def _append_event(path: Path, event: dict) -> None:
    line = json.dumps(event, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(line)


def main():
    setup_logging()
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
            logging.warning("%s", msg)
        else:
            logging.info("temp %.1f°C", temp_c)
    except OSError as e:
        logging.error("Cannot read temperature: %s", e)

    # Available memory
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                mem_mb = round(int(line.split()[1]) / 1024, 1)
                sample["memory_available_mb"] = mem_mb
                if mem_mb <= warn_mem_mb:
                    msg = f"{mem_mb:.0f} MB available (below threshold {warn_mem_mb:.0f} MB)"
                    _append_event(HEALTH_EVENTS, {"ts": now, "level": "WARN", "source": "memory", "msg": msg})
                    logging.warning("%s", msg)
                break
    except (OSError, ValueError) as e:
        logging.error("Cannot read memory info: %s", e)

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

    # IPv4 routing probe (no packets sent; fails if kernel has no default route)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        s.close()
        sample["network_ipv4_ok"] = 1
    except Exception:
        sample["network_ipv4_ok"] = 0

    # Minutes since last successful R2 upload (from push_events.py state)
    try:
        health_state = json.loads(HEALTH_STATE_FILE.read_text(encoding="utf-8"))
        last_upload = health_state.get("last_successful_upload_at")
        if last_upload:
            last_dt = datetime.fromisoformat(last_upload)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            staleness = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
            sample["upload_staleness_min"] = round(staleness, 1)
    except (json.JSONDecodeError, OSError, ValueError):
        pass

    # Disk usage
    def _disk_stat(path: str) -> tuple[float, float]:
        st = os.statvfs(path)
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        used_pct = round(used / (used + avail) * 100, 1) if (used + avail) else 0.0
        return used_pct, round(avail / 1024 ** 3, 2)

    try:
        used_pct, free_gb = _disk_stat("/")
        sample["disk_root_used_pct"] = used_pct
        sample["disk_root_free_gb"] = free_gb
    except OSError:
        pass

    backup_dest = os.environ.get("BACKUP_DEST")
    if backup_dest:
        try:
            if os.stat(backup_dest).st_dev != os.stat("/").st_dev:
                used_pct, free_gb = _disk_stat(backup_dest)
                sample["disk_backup_used_pct"] = used_pct
                sample["disk_backup_free_gb"] = free_gb
        except OSError:
            pass

    # Disk I/O delta (MB read/written since last sample)
    try:
        mstate: dict = {}
        if METRICS_STATE_FILE.exists():
            mstate = json.loads(METRICS_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        mstate = {}

    root_dev = _root_block_device()
    cur_io = _read_diskstats(root_dev) if root_dev else None
    if cur_io is not None:
        prev_io = mstate.get("diskstats")
        if (
            prev_io and len(prev_io) == 2
            and mstate.get("device") == root_dev
        ):
            _SECTORS_PER_MB = 2048  # 512 bytes/sector
            read_mb = round((cur_io[0] - prev_io[0]) / _SECTORS_PER_MB, 2)
            write_mb = round((cur_io[1] - prev_io[1]) / _SECTORS_PER_MB, 2)
            if read_mb >= 0 and write_mb >= 0:
                sample["disk_io_read_mb"] = read_mb
                sample["disk_io_write_mb"] = write_mb

        try:
            new_mstate = {"device": root_dev, "diskstats": list(cur_io)}
            tmp = METRICS_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(new_mstate), encoding="utf-8")
            os.replace(tmp, METRICS_STATE_FILE)
        except OSError as e:
            logging.warning("Could not save disk I/O state: %s", e)

    _append_event(METRIC_SAMPLES, sample)
    logging.info("sample written (%s)", ", ".join(f"{k}={v}" for k, v in sample.items() if k != "ts"))

    # Service health checks
    try:
        result = subprocess.run(
            ["systemctl", "is-active"] + _SYSTEMCTL_SERVICES,
            capture_output=True, text=True, timeout=15,
        )
        statuses = result.stdout.strip().splitlines()
        for svc, status in zip(_SYSTEMCTL_SERVICES, statuses):
            if status != "active":
                msg = f"Service {svc} is {status}"
                _append_event(HEALTH_EVENTS, {"ts": now, "level": "ERROR", "source": "services", "msg": msg})
                logging.error("%s", msg)
    except Exception as e:
        logging.error("Cannot check service status: %s", e)

    for svc, (host, port) in _TCP_SERVICES.items():
        if not _tcp_check(host, port):
            msg = f"Service {svc} not reachable on port {port}"
            _append_event(HEALTH_EVENTS, {"ts": now, "level": "ERROR", "source": "services", "msg": msg})
            logging.error("%s", msg)

    # Gateway reachability
    gw = _default_gateway()
    if gw is None:
        logging.warning("Could not determine default gateway from /proc/net/route")
    elif not _ping(gw):
        msg = f"Default gateway {gw} is unreachable"
        _append_event(HEALTH_EVENTS, {"ts": now, "level": "ERROR", "source": "network", "msg": msg})
        logging.error("%s", msg)
    else:
        logging.info("Gateway %s reachable", gw)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Unexpected error")
        sys.exit(1)
