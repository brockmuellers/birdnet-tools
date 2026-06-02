# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in this repository.

## What this repo does

Contains auxiliary tools for a BirdNET-Pi installation on a Raspberry Pi. The repo is intended to be used directly on the Raspberry Pi.

Currently, this repository:

- Exports BirdNET-Pi detection data from a local SQLite database to Cloudflare R2 every 15 minutes (`scripts/export_data.py`). The exported JSON contains:
	- All bird detections from the last 7 days (`recent_observations`)
	- All-time per-species counts broken down by month and 15-minute time-of-day bucket (`monthly_stats`)
- Queries the systemd journal for detections excluded by the species frequency threshold and displays them with their current-week frequency scores (`scripts/excluded_detections.py`)
- Backs up BirdNET-Pi data to a local drive (`scripts/run_backup.sh`):
	- Nightly: copies `birds.db` via SQLite's online backup API (safe under concurrent writes), retaining the last N days
	- Weekly: wraps BirdNET-Pi's `backup_data.sh` to produce a full tar of config + DB + audio clips + spectrograms; pauses BirdNET-Pi services during the run
- Backs up `birds.db` to Cloudflare R2 nightly (`scripts/backup_db_r2.py`). Aborts with an error if the DB exceeds a configurable size ceiling; warns at 80% of that ceiling. Uses SQLite's online backup API for a safe snapshot, then uploads via SigV4.
- Refreshes the species frequency cache weekly (`scripts/refresh_species_freq.py`). Queries the BirdNET-Pi metadata model for all ~6,500 species at the current week and location; writes `.species_freq_cache.json` listing species with frequency > 0. Used by `push_events.py` to suppress exclusion events for zero-frequency species.
- Aggregates events and pushes them to Cloudflare R2 every 15 minutes (`scripts/push_events.py`). Sources:
	- `birdnet_analysis` systemd journal: species frequency exclusions with non-zero frequency (INFO) and errors (ERROR); exclusions for species with a `0.0` frequency score (birds not expected in this region) are suppressed using `.species_freq_cache.json`
	- `logs/export.log` / `logs/backup.log`: WARN and ERROR lines emitted by cron scripts
	- `logs/failures.log`: non-zero exit codes captured by `run_cron.sh`
	- `health_events.jsonl`: WARN/ERROR lines from `logs/health.log`; temp threshold WARNs, low-memory WARNs, and service-down ERRORs from `scripts/sample_metrics.py`; WARN when `primary_ip` changes between runs (indicates DHCP reassignment that breaks `.local` mDNS resolution)
	- `metric_samples.jsonl`: periodic numeric samples from `scripts/sample_metrics.py` (every 5 min): `temp_c`, `memory_available_mb`, `wifi_signal_dbm`, `disk_root_used_pct`, `disk_root_free_gb`, `disk_backup_used_pct`, `disk_backup_free_gb`
	- Events are stored locally in `health_events.jsonl`, `birdnet_events.jsonl`, and `cron_events.jsonl`, pruned to `EVENT_LOG_RETAIN_DAYS`, merged by timestamp, and uploaded to R2. Metric samples are bucketed into hourly windows (min/max/avg per key) and uploaded as `metric_windows`.

## Running

```bash
scripts/export_data.py                         # manual test run for export; reads .env automatically
scripts/backup_db_r2.py                        # manual test run for DB R2 backup; reads .env automatically
scripts/run_backup.sh --db                     # nightly DB backup (test before enabling cron)
scripts/run_backup.sh --full                   # weekly full backup (pauses BirdNET-Pi services)
scripts/sample_metrics.py                      # manual test run for metric sampling; reads .env automatically
scripts/push_events.py                         # manual test run for event push; reads .env automatically
scripts/refresh_species_freq.py                # refresh species frequency cache (weekly; uses BirdNET-Pi venv)
python3 scripts/excluded_detections.py         # show frequency-excluded detections (last 7 days)
python3 scripts/excluded_detections.py --days 14
```

`export_data.py`, `backup_db_r2.py`, `run_backup.sh`, `sample_metrics.py`, and `push_events.py` all use `flock` to prevent overlapping cron runs.

`run_cron.sh` is a thin wrapper used in all cron entries. It runs the given command and appends an ERROR line to `logs/failures.log` if the exit code is non-zero, enabling `push_events.py` to surface cron failures alongside other events.

`excluded_detections.py` reads from the `birdnet_analysis` systemd journal and queries the BirdNET-Pi metadata model via `~/BirdNET-Pi/birdnet/bin/python3` (the BirdNET-Pi venv, which has TensorFlow Lite). It does not need the `.env` file.

## Environment

Copy `.env.example` to `.env` and fill in credentials. Required variables:
- `BIRDNETPI_DB_PATH` — path to BirdNET-Pi's `birds.db` SQLite file
- `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` — Cloudflare R2 credentials
- `R2_OBJECT_KEY` — defaults to `birdnet-data.json`
- `EXPORT_OUTPUT_PATH` — defaults to `/tmp/birdnet_export.json`
- `BACKUP_DEST` — destination directory for backups (required by `run_backup.sh`)
- `BACKUP_DB_RETAIN_DAYS` — days of nightly DB backups to keep (default: 7)
- `BACKUP_DISK_WARN_PCT` — log a WARN when backup disk exceeds this % full (default: 80)
- `R2_DB_BACKUP_MAX_MB` — **required** by `backup_db_r2.py`; backup aborts if DB exceeds this many MB; warn logged at 80% of limit (note: upload reads the snapshot into memory, so allow ~2–3× the DB size in free RAM)
- `R2_DB_BACKUP_OBJECT_KEY` — R2 object key for the DB backup (default: `birds.db`); overwritten nightly (no history kept in R2)
- `TEMP_WARN_C` — log a WARN when chip temperature exceeds this value (default: 70°C)
- `MEMORY_WARN_MB` — log a WARN when available memory falls below this value (default: 100 MB)
- `EVENT_LOG_RETAIN_DAYS` — days of events to keep locally and upload (default: 7)
- `EVENT_LOG_OBJECT_KEY` — R2 object key for the event log (default: `birdnet-events.json`)

## Key design constraints

- **Minimal third-party dependencies.** The R2 upload is implemented with stdlib `urllib` and a hand-rolled AWS SigV4 signing implementation in `scripts/_r2.py` (shared by `export_data.py`, `backup_db_r2.py`, and `push_events.py`). Avoiding introducing `boto3` or any other external package — installing packages on the Pi is intentionally avoided.
- The database is opened read-only via SQLite URI (`file:...?mode=ro`).
- JSON is written atomically: written to a `.tmp` file first, then renamed with `os.replace`.
- DB backups use `sqlite3.Connection.backup()` (stdlib), not `cp`. Plain file copy is unsafe on a live SQLite database: it reads at the OS level without respecting SQLite's locking, risking torn writes; it also misses pending WAL-mode writes in the `-wal` sidecar.
- Full backups write to `birdnet-full-backup.tar.tmp` and rename only on success, so a failed backup never destroys the previous good copy.

## Documentation

**Keep `README.md` in sync when scripts change.** Each script has its own section in the README covering prerequisites, setup, and behavior. When a script is renamed, added, removed, or its behavior changes (metrics sampled, events written, payload shape, env vars, cron label), update the corresponding README section in the same commit. Also update the cron table in the README's "Cron jobs" section and the payload shape example under `push_events.py` if the R2 JSON structure changes.

## Context

When more context is required to understand the functionality of BirdNET-Pi, its repo may be found at `../BirdNET-Pi/`.

## Logging conventions

Python scripts use `setup_logging()` from `scripts/_utils.py`. It configures Python's stdlib `logging` module with the format `[timestamp] LEVEL: message` on stdout, mapping `WARNING` → `WARN` to match `push_events.py`'s `_LEVEL_RE` parser. All Python scripts call `setup_logging()` near startup and wrap `main()` in a top-level `try/except Exception: logging.exception(...)` so that unhandled crashes produce an `ERROR:` line that `push_events.py` can surface.

`push_events.py` scans log files (`export.log`, `backup.log`, `health.log`, `failures.log`) for lines matching `\b(WARN|ERROR):\s*(.*)`. Only lines with those prefixes are surfaced in the R2 event log — bare stderr output without a level prefix is ignored.

Bash scripts (`run_backup.sh`) do not yet emit structured `ERROR:`-prefixed output. Bare command errors (e.g., from `stat`, `mkdir`) end up in the log file but are not surfaced in the event log. New bash scripts should either use a similar `log "ERROR: ..."` convention or redirect stderr through a structured prefix.

## Cron jobs

All entries are wrapped with `run_cron.sh LABEL` so non-zero exit codes are appended to `logs/failures.log` and surfaced in the R2 event log. Set `REPO` once at the top of your crontab:

```
REPO=/home/sara/repos/birdnet-tools

*/15 * * * * $REPO/scripts/run_cron.sh export       timeout 10m $REPO/scripts/export_data.py              >> $REPO/logs/export.log  2>&1
0 2 * * *   $REPO/scripts/run_cron.sh db-backup     timeout 30m $REPO/scripts/run_backup.sh --db           >> $REPO/logs/backup.log  2>&1
0 3 * * 0   $REPO/scripts/run_cron.sh full-backup   timeout 2h  $REPO/scripts/run_backup.sh --full          >> $REPO/logs/backup.log  2>&1
30 2 * * *  $REPO/scripts/run_cron.sh db-r2-backup  timeout 30m $REPO/scripts/backup_db_r2.py               >> $REPO/logs/backup.log  2>&1
*/5 * * * * $REPO/scripts/run_cron.sh metrics       timeout 30  $REPO/scripts/sample_metrics.py            >> $REPO/logs/health.log  2>&1
*/15 * * * * $REPO/scripts/run_cron.sh push-events    timeout 10m $REPO/scripts/push_events.py               >> $REPO/logs/health.log  2>&1
0 1 * * 1    $REPO/scripts/run_cron.sh species-freq  timeout 5m  $REPO/scripts/refresh_species_freq.py         >> $REPO/logs/health.log  2>&1
```
