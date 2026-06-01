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

## Running

```bash
scripts/export_data.py                         # manual test run for export; reads .env automatically
scripts/backup_db_r2.py                        # manual test run for DB R2 backup; reads .env automatically
scripts/run_backup.sh --db                     # nightly DB backup (test before enabling cron)
scripts/run_backup.sh --full                   # weekly full backup (pauses BirdNET-Pi services)
python3 scripts/excluded_detections.py         # show frequency-excluded detections (last 7 days)
python3 scripts/excluded_detections.py --days 14
```

`export_data.py`, `backup_db_r2.py`, and `run_backup.sh` all use `flock` to prevent overlapping cron runs.

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

## Key design constraints

- **Minimal third-party dependencies.** The R2 upload is implemented with stdlib `urllib` and a hand-rolled AWS SigV4 signing implementation in `scripts/_r2.py` (shared by `export_data.py` and `backup_db_r2.py`). Avoiding introducing `boto3` or any other external package — installing packages on the Pi is intentionally avoided.
- The database is opened read-only via SQLite URI (`file:...?mode=ro`).
- JSON is written atomically: written to a `.tmp` file first, then renamed with `os.replace`.
- DB backups use `sqlite3.Connection.backup()` (stdlib), not `cp`. Plain file copy is unsafe on a live SQLite database: it reads at the OS level without respecting SQLite's locking, risking torn writes; it also misses pending WAL-mode writes in the `-wal` sidecar.
- Full backups write to `birdnet-full-backup.tar.tmp` and rename only on success, so a failed backup never destroys the previous good copy.

## Context

When more context is required to understand the functionality of BirdNET-Pi, its repo may be found at `../BirdNET-Pi/`.

## Cron jobs

```
*/15 * * * * timeout 10m  /home/sara/repos/birdnet-tools/scripts/export_data.py >> /home/sara/repos/birdnet-tools/export.log 2>&1
0 2 * * *   timeout 30m  /home/sara/repos/birdnet-tools/scripts/run_backup.sh --db   >> /home/sara/repos/birdnet-tools/backup.log 2>&1
0 3 * * 0   timeout 2h   /home/sara/repos/birdnet-tools/scripts/run_backup.sh --full >> /home/sara/repos/birdnet-tools/backup.log 2>&1
30 2 * * *  timeout 30m  /home/sara/repos/birdnet-tools/scripts/backup_db_r2.py      >> /home/sara/repos/birdnet-tools/backup.log 2>&1
```
