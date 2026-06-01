# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in this repository.

## What this repo does

Contains auxiliary tools for a BirdNET-Pi installation on a Raspberry Pi. The repo is intended to be used directly on the Raspberry Pi.

Currently, this repository:

- Exports BirdNET-Pi detection data from a local SQLite database to Cloudflare R2 every 15 minutes. The exported JSON contains:
	- All bird detections from the last 7 days (`recent_observations`)
	- All-time per-species counts broken down by month and 15-minute time-of-day bucket (`monthly_stats`)
- Queries the systemd journal for detections excluded by the species frequency threshold and displays them with their current-week frequency scores (`scripts/excluded_detections.py`)

## Running

```bash
scripts/run_export.sh                          # manual test run for export; reads .env automatically
python3 scripts/excluded_detections.py         # show frequency-excluded detections (last 7 days)
python3 scripts/excluded_detections.py --days 14
```

`run_export.sh` uses `flock` to prevent overlapping cron runs.

`excluded_detections.py` reads from the `birdnet_analysis` systemd journal and queries the BirdNET-Pi metadata model via `~/BirdNET-Pi/birdnet/bin/python3` (the BirdNET-Pi venv, which has TensorFlow Lite). It does not need the `.env` file.

## Environment

Copy `.env.example` to `.env` and fill in credentials. Required variables:
- `BIRDNETPI_DB_PATH` — path to BirdNET-Pi's `birds.db` SQLite file
- `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` — Cloudflare R2 credentials
- `R2_OBJECT_KEY` — defaults to `birdnet-data.json`
- `EXPORT_OUTPUT_PATH` — defaults to `/tmp/birdnet_export.json`

## Key design constraints

- **Minimal third-party dependencies.** The R2 upload is implemented with stdlib `urllib` and a hand-rolled AWS SigV4 signing implementation (`_hmac_sha256`, `_signing_key`, `upload_to_r2` in `export_data.py`). Avoiding introducing `boto3` or any other external package — installing packages on the Pi is intentionally avoided.
- The database is opened read-only via SQLite URI (`file:...?mode=ro`).
- JSON is written atomically: written to a `.tmp` file first, then renamed with `os.replace`.

## Context

When more context is required to understand the functionality of BirdNET-Pi, its repo may be found at `../BirdNET-Pi/`.

## Cron job

```
*/15 * * * * /home/sara/repos/birdnet-tools/scripts/run_export.sh >> /home/sara/repos/birdnet-tools/export.log 2>&1
```
