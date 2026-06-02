# birdnet-tools
Tools for displaying and manipulating data from a [BirdNET-Pi](https://github.com/Nachtzuster/BirdNET-Pi).

This repo should be cloned into the Raspberry Pi that's running your BirdNET installation.

---

## Data exploration

---

### excluded_detections.py

Shows detections that were excluded because they fell below the [species occurrence frequency threshold](https://github.com/Nachtzuster/BirdNET-Pi/wiki/Settings#species-occurrence-frequency-threshold), along with the current-week frequency score for each species.

#### Prerequisites

- BirdNET-Pi installed at `~/BirdNET-Pi` (uses its Python venv and metadata model)
- `journalctl` access to the `birdnet_analysis` systemd service

#### Usage

```
python3 scripts/excluded_detections.py [--days N]
```

`--days` defaults to 7. The frequency values are looked up from the metadata model using the **current** week of year; for `--days` values that span more than two ISO weeks, the script prints a warning explaining the potential discrepancy.

---

## Cron jobs

All cron entries use `run_cron.sh` as a wrapper. It passes the exit code through and appends an `ERROR` line to `logs/failures.log` on failure, which `push_events.py` picks up and surfaces in the R2 event log.

**Logging conventions:** Python scripts use `setup_logging()` from `scripts/_utils.py`, which configures Python's stdlib `logging` module to emit `[timestamp] LEVEL: message` lines on stdout. `push_events.py` scans log files for `WARN:` and `ERROR:` prefixed lines; only lines with those prefixes are surfaced in the R2 event log. Unhandled exceptions are caught at `__main__` and logged via `logging.exception()`, ensuring crashes produce an `ERROR:` line that gets picked up. Bash scripts (`run_backup.sh`) do not yet have equivalent structured error output — bare stderr from failed commands lands in the log file without a level prefix and is not surfaced in the event log.

`install_cron.sh` installs all jobs at once and is safe to re-run — it removes any existing birdnet-tools entries (including old absolute-path style entries) before adding the current set:

```
chmod +x scripts/install_cron.sh
scripts/install_cron.sh
```

It detects the repo path automatically from its own location, so no manual path editing is needed. After running, it prints the full updated crontab for review.

---

### run_backup.sh

Backs up BirdNET-Pi data to a local drive (e.g., a USB stick) in two complementary ways:

- **Nightly DB backup** — copies `birds.db` using SQLite's online backup API, keeping the last N days (default 7, configurable). Safe to run while BirdNET-Pi is actively recording: it acquires brief page-level read locks rather than holding an exclusive lock for the whole copy, so detections are never blocked. This is why a plain `cp` is not used — it reads at the OS level with no awareness of SQLite's locking, and can produce a torn or incomplete file if a write happens mid-copy (or miss pending WAL-mode writes entirely).

- **Weekly full backup** — wraps BirdNET-Pi's own `backup_data.sh`, which bundles your config, the full detection database, all extracted audio clips, and spectrogram charts into a single tar. Only one copy is kept (no rotation). **BirdNET-Pi services pause during this backup** — typically a few minutes for a large archive — so schedule it during low-activity hours. If the backup fails for any reason, the previous tar is preserved (the script writes to a `.tmp` file and only replaces the live backup on success).

#### Size expectations

A year of data at ~500 detections/day produces roughly 20 GB in the full backup: audio clips (MP3, ~6 seconds each) account for most of it (~14 GB), spectrograms (PNG) another ~8 GB, and `birds.db` is under 100 MB. A 64 GB USB drive is a comfortable target; 32 GB would be tight with a year's worth of clips.

If disk space is constrained, the nightly DB-only backup is a much leaner option — it captures the complete detection record (timestamps, species, confidence, all metadata) at under 100 MB, and is sufficient to restore full analysis history. You lose the audio clips and spectrograms, but those aren't needed for the detection data itself.

#### Prerequisites

- A writable destination directory (USB drive, NFS mount, etc.) set as `BACKUP_DEST` in `.env`
- BirdNET-Pi installed at `~/BirdNET-Pi` (the full backup uses its `backup_data.sh`)

#### Setup

1. Add backup settings to `.env`:
   ```
   BACKUP_DEST=/mnt/usb/birdnet-backup
   BACKUP_DB_RETAIN_DAYS=7   # optional, default 7
   # BACKUP_DISK_WARN_PCT=80  # optional, default 80
   ```

2. Make the script executable:
   ```
   chmod +x scripts/run_backup.sh
   ```

3. Run manually once to verify both modes work before trusting cron:
   ```
   scripts/run_backup.sh --db
   scripts/run_backup.sh --full
   ```

The nightly DB backup runs at 2am daily; the full backup runs at 3am on Sundays. Both log to `logs/backup.log`. A `WARN` line is written to the log when the backup disk exceeds the fill threshold (default 80%).

Run `scripts/install_cron.sh` to register the cron jobs (see [Cron jobs](#cron-jobs)).

#### Restoring after SD card failure

1. Fresh-install BirdNET-Pi on the new card using its installer
2. Run:
   ```
   ~/BirdNET-Pi/scripts/backup_data.sh -a restore -f /mnt/usb/birdnet-backup/birdnet-full-backup.tar
   ```
3. Re-clone this repo and restore `.env` from wherever you keep credentials

If you only have a DB backup (no full tar), copy the `.db` file into place before starting BirdNET-Pi:
```
cp /mnt/usb/birdnet-backup/birds-db/birds-YYYY-MM-DD.db ~/BirdNET-Pi/scripts/birds.db
```

---

### backup_db_r2.py

Backs up `birds.db` to Cloudflare R2 nightly using SQLite's online backup API — the same safe snapshot approach as the local DB backup, but uploaded to R2 for off-site storage. The object is overwritten each run (no history kept in R2); pair it with the local backup if you want rotation.

Before uploading, the script checks the DB size against a configurable ceiling (`R2_DB_BACKUP_MAX_MB`). It aborts with an error if the ceiling is exceeded, and logs a warning once the DB reaches 80% of the limit. The snapshot is written to `/var/tmp` (not `/tmp`) to avoid doubling DB size in RAM on Pis where `/tmp` is a ramdisk.

#### Prerequisites

- Python 3 (no third-party packages required)
- A Cloudflare R2 bucket with an API token that has Object Read & Write permissions

#### Setup

1. Add these variables to your `.env`:
   ```
   R2_DB_BACKUP_MAX_MB=500          # required; abort threshold in MB
   # R2_DB_BACKUP_OBJECT_KEY=birds.db  # optional, this is the default
   ```

2. Make the script executable:
   ```
   chmod +x scripts/backup_db_r2.py
   ```

3. Run manually once to verify it works before trusting cron:
   ```
   scripts/backup_db_r2.py
   ```

Scheduled at 2:30am — after the nightly local DB backup at 2:00am.

Run `scripts/install_cron.sh` to register the cron job (see [Cron jobs](#cron-jobs)).

---

### export_data.py

Exports BirdNET-Pi detection data to a JSON file and uploads it to Cloudflare R2 every 15 minutes. The JSON contains all observations from the last 7 days plus all-time per-species observation counts broken down by month and 15-minute time-of-day bucket.

#### Prerequisites

- Python 3 (no third-party packages required — the R2 upload uses stdlib `urllib` with a manual [AWS SigV4](https://docs.aws.amazon.com/general/latest/gr/signature-version-4.html) signing implementation, avoiding the need to install `boto3` on the Pi)
- A Cloudflare R2 bucket with an API token that has Object Read & Write permissions

#### Setup

1. Copy the example env file and fill in your credentials:
   ```
   cp .env.example .env
   ```

2. Make the script executable:
   ```
   chmod +x scripts/export_data.py
   ```

#### Test manually

```
scripts/export_data.py
```

Check the log output, then verify the object appears in your R2 bucket in the Cloudflare dashboard.

To make the JSON publicly accessible, enable **Allow Public Access** on the bucket in the Cloudflare dashboard.

Run `scripts/install_cron.sh` to register the cron job (see [Cron jobs](#cron-jobs)).

---

### sample_metrics.py

Samples system metrics every 5 minutes and appends a record to `metric_samples.jsonl`. Also writes discrete events to `health_events.jsonl` when thresholds are exceeded or services are down.

**Metrics sampled** (numeric, used by `push_events.py` to build hourly min/max/avg windows):
- `temp_c` — chip temperature
- `memory_available_mb` — available RAM
- `wifi_signal_dbm` — WiFi signal strength (first wireless interface)
- `disk_root_used_pct`, `disk_root_free_gb` — root filesystem usage
- `disk_backup_used_pct`, `disk_backup_free_gb` — backup mount usage (only when `BACKUP_DEST` is set and on a different device than `/`)
- `disk_io_read_mb`, `disk_io_write_mb` — MB read/written on the root device since the previous sample (delta between consecutive `/proc/diskstats` readings; absent on the first run after a restart)

**Events written to `health_events.jsonl`:**
- `WARN` if chip temperature exceeds `TEMP_WARN_C` (default 70°C)
- `WARN` if available memory falls below `MEMORY_WARN_MB` (default 100 MB)
- `ERROR` for each monitored service that is not active (`birdnet_analysis`, `birdnet_recording`, `birdnet_stats`, `caddy`, `ssh`)

#### Prerequisites

- Python 3
- `/sys/class/thermal/thermal_zone0/temp` readable (standard on Raspberry Pi)
- `systemctl` available

#### Setup

Optionally add to `.env`:
```
# TEMP_WARN_C=70     # optional, this is the default
# MEMORY_WARN_MB=100  # optional, this is the default
```

Make the script executable:
```
chmod +x scripts/sample_metrics.py
```

Run manually once to verify:
```
scripts/sample_metrics.py
```

Run `scripts/install_cron.sh` to register the cron job (see [Cron jobs](#cron-jobs)).

---

### push_events.py

Aggregates events from multiple sources every 15 minutes, stores them locally in two JSONL files, and uploads a merged JSON to Cloudflare R2. The R2 object is intended for consumption by an external viewer that can color-code events by severity.

**Sources:**
- `birdnet_analysis` systemd journal — species frequency exclusions (logged as `INFO`) and `[ERROR]`-level messages
- `logs/export.log` / `logs/backup.log` — `WARN` and `ERROR` lines emitted by cron scripts
- `logs/failures.log` — non-zero exit codes written by `run_cron.sh`
- `health_events.jsonl` — temp threshold warnings, service-down errors from `sample_metrics.py`, and IP-change warnings when `primary_ip` shifts between runs (indicates DHCP reassignment that breaks `.local` mDNS)
- `metric_samples.jsonl` — numeric samples written by `sample_metrics.py` every 5 minutes

**Local storage:** events accumulate in `health_events.jsonl`, `birdnet_events.jsonl`, and `cron_events.jsonl`, pruned to `EVENT_LOG_RETAIN_DAYS` (default 7). Metric samples are stored in `metric_samples.jsonl` and bucketed into hourly min/max/avg windows for upload.

**Event log cap:** the `events` array is capped at 5000 entries (most recent first). If the cap is exceeded, a synthetic `ERROR` event is appended at the end indicating how many older events were omitted.

**R2 payload shape:**
```json
{
  "generated_at": "2026-06-01T12:00:00+00:00",
  "timezone": "America/Los_Angeles",
  "health": {
    "last_successful_upload_at": "...",
    "disk": {"/": {"used_pct": 42.1, "free_gb": 12.3}},
    "db_size_mb": 87.4,
    "uptime_seconds": 123456,
    "temp_c": 51.2,
    "memory_available_mb": 612.0,
    "wifi_signal_dbm": -58,
    "hostname": "echo-birdnet",
    "primary_ip": "192.168.1.42"
  },
  "events": [
    {"ts": "...", "level": "INFO",  "source": "birdnet",   "msg": "Excluded: Black-crowned Night-Heron (Nycticorax nycticorax)"},
    {"ts": "...", "level": "WARN",  "source": "temp",      "msg": "72.1°C (exceeds threshold 70°C)"},
    {"ts": "...", "level": "ERROR", "source": "cron",      "msg": "export exited with code 1"},
    {"ts": "...", "level": "ERROR", "source": "services",  "msg": "Service birdnet_analysis is inactive"},
    {"ts": "...", "level": "WARN",  "source": "network",   "msg": "primary_ip changed from 192.168.1.42 to 192.168.1.55 — mDNS hostname (.local) may no longer resolve correctly"}
  ],
  "metric_windows": [
    {"window_start": "2026-06-01T11:00:00+00:00", "disk_io_read_mb": {"min": 0.5, "max": 3.2, "avg": 1.4}, "disk_io_write_mb": {"min": 0.1, "max": 1.8, "avg": 0.6}, "disk_root_free_gb": {"min": 12.1, "max": 12.3, "avg": 12.2}, "disk_root_used_pct": {"min": 41.8, "max": 42.1, "avg": 42.0}, "memory_available_mb": {"min": 580.0, "max": 640.0, "avg": 610.5}, "temp_c": {"min": 49.1, "max": 53.4, "avg": 51.2}, "wifi_signal_dbm": {"min": -65, "max": -55, "avg": -60.0}}
  ],
  "last_hour": {"disk_io_read_mb": {"min": 0.5, "max": 3.2, "avg": 1.4}, "disk_io_write_mb": {"min": 0.1, "max": 1.8, "avg": 0.6}, "disk_root_used_pct": {"min": 41.8, "max": 42.1, "avg": 42.0}, "memory_available_mb": {"min": 580.0, "max": 640.0, "avg": 610.5}, "temp_c": {"min": 49.1, "max": 53.4, "avg": 51.2}, "wifi_signal_dbm": {"min": -65, "max": -55, "avg": -60.0}},
  "last_day":  {"disk_io_read_mb": {"min": 0.2, "max": 8.1, "avg": 1.6}, "disk_io_write_mb": {"min": 0.0, "max": 5.4, "avg": 0.7}, "disk_root_used_pct": {"min": 41.5, "max": 42.1, "avg": 41.8}, "memory_available_mb": {"min": 420.0, "max": 680.0, "avg": 595.0}, "temp_c": {"min": 46.0, "max": 61.2, "avg": 52.1}, "wifi_signal_dbm": {"min": -72, "max": -52, "avg": -61.0}}
}
```

#### Prerequisites

- Python 3 (no third-party packages required)
- A Cloudflare R2 bucket with an API token that has Object Read & Write permissions

#### Setup

1. Ensure the shared R2 credentials are already in `.env` (same bucket as `export_data.py`).

2. Optionally add to `.env`:
   ```
   # EVENT_LOG_RETAIN_DAYS=7              # optional, this is the default
   # EVENT_LOG_OBJECT_KEY=birdnet-events.json  # optional, this is the default
   ```

3. Make the scripts executable:
   ```
   chmod +x scripts/push_events.py scripts/run_cron.sh
   ```

4. Run manually once to verify:
   ```
   scripts/push_events.py
   ```

Run `scripts/install_cron.sh` to register the cron job (see [Cron jobs](#cron-jobs)).

---

### refresh_species_freq.py

Queries the BirdNET-Pi metadata model for all ~6,500 species at the current week of year and your configured lat/lon, then writes `.species_freq_cache.json` at the repo root. The cache lists every species with a non-zero frequency score for your region this week.

`push_events.py` reads this cache to suppress frequency-exclusion events for species that score exactly `0.0` — birds that would never be expected in your region and whose exclusions add no signal to the event log. If the cache is absent or unreadable, `push_events.py` falls back to recording all exclusions (same behavior as before this script existed).

Frequencies are scored per ISO week of year, so the cache is refreshed every Monday at 1am to stay in sync with the model's seasonal scores.

#### Prerequisites

- BirdNET-Pi installed at `~/BirdNET-Pi` (uses its Python venv and metadata model)

#### Usage

```
scripts/refresh_species_freq.py
```

Run `scripts/install_cron.sh` to register the cron job (see [Cron jobs](#cron-jobs)).
