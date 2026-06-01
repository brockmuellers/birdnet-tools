# birdnet-tools
Tools for displaying and manipulating data from a [BirdNET-Pi](https://github.com/Nachtzuster/BirdNET-Pi).

This repo should be cloned into the Raspberry Pi that's running your BirdNET installation.

## excluded_detections.py

Shows detections that were excluded because they fell below the [species occurrence frequency threshold](https://github.com/Nachtzuster/BirdNET-Pi/wiki/Settings#species-occurrence-frequency-threshold), along with the current-week frequency score for each species.

### Prerequisites

- BirdNET-Pi installed at `~/BirdNET-Pi` (uses its Python venv and metadata model)
- `journalctl` access to the `birdnet_analysis` systemd service

### Usage

```
python3 scripts/excluded_detections.py [--days N]
```

`--days` defaults to 7. The frequency values are looked up from the metadata model using the **current** week of year; for `--days` values that span more than two ISO weeks, the script prints a warning explaining the potential discrepancy.

---

## run_backup.sh

Backs up BirdNET-Pi data to a local drive (e.g., a USB stick) in two complementary ways:

- **Nightly DB backup** — copies `birds.db` using SQLite's online backup API, keeping the last N days (default 7, configurable). Safe to run while BirdNET-Pi is actively recording: it acquires brief page-level read locks rather than holding an exclusive lock for the whole copy, so detections are never blocked. This is why a plain `cp` is not used — it reads at the OS level with no awareness of SQLite's locking, and can produce a torn or incomplete file if a write happens mid-copy (or miss pending WAL-mode writes entirely).

- **Weekly full backup** — wraps BirdNET-Pi's own `backup_data.sh`, which bundles your config, the full detection database, all extracted audio clips, and spectrogram charts into a single tar. Only one copy is kept (no rotation). **BirdNET-Pi services pause during this backup** — typically a few minutes for a large archive — so schedule it during low-activity hours. If the backup fails for any reason, the previous tar is preserved (the script writes to a `.tmp` file and only replaces the live backup on success).

### Size expectations

A year of data at ~500 detections/day produces roughly 20 GB in the full backup: audio clips (MP3, ~6 seconds each) account for most of it (~14 GB), spectrograms (PNG) another ~8 GB, and `birds.db` is under 100 MB. A 64 GB USB drive is a comfortable target; 32 GB would be tight with a year's worth of clips.

If disk space is constrained, the nightly DB-only backup is a much leaner option — it captures the complete detection record (timestamps, species, confidence, all metadata) at under 100 MB, and is sufficient to restore full analysis history. You lose the audio clips and spectrograms, but those aren't needed for the detection data itself.

### Prerequisites

- A writable destination directory (USB drive, NFS mount, etc.) set as `BACKUP_DEST` in `.env`
- BirdNET-Pi installed at `~/BirdNET-Pi` (the full backup uses its `backup_data.sh`)

### Setup

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

### Install cron jobs

```
crontab -e
```

Add these lines:
```
0 2 * * *   /home/sara/repos/birdnet-tools/scripts/run_backup.sh --db   >> /home/sara/repos/birdnet-tools/backup.log 2>&1
0 3 * * 0   /home/sara/repos/birdnet-tools/scripts/run_backup.sh --full >> /home/sara/repos/birdnet-tools/backup.log 2>&1
```

The nightly DB backup runs at 2am daily; the full backup runs at 3am on Sundays. Both log to `backup.log`. A `WARN` line is written to the log when the backup disk exceeds the fill threshold (default 80%).

### Restoring after SD card failure

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

## export_data.py

Exports BirdNET-Pi detection data to a JSON file and uploads it to Cloudflare R2 every 15 minutes. The JSON contains all observations from the last 7 days plus all-time per-species observation counts broken down by month and 15-minute time-of-day bucket.

### Prerequisites

- Python 3 (no third-party packages required — the R2 upload uses stdlib `urllib` with a manual [AWS SigV4](https://docs.aws.amazon.com/general/latest/gr/signature-version-4.html) signing implementation, avoiding the need to install `boto3` on the Pi)
- A Cloudflare R2 bucket with an API token that has Object Read & Write permissions

### Setup

1. Copy the example env file and fill in your credentials:
   ```
   cp .env.example .env
   ```

2. Make the cron wrapper executable:
   ```
   chmod +x scripts/run_export.sh
   ```

### Test manually

```
scripts/run_export.sh
```

Check the log output, then verify the object appears in your R2 bucket in the Cloudflare dashboard.

To make the JSON publicly accessible, enable **Allow Public Access** on the bucket in the Cloudflare dashboard.

### Install cron job

```
crontab -e
```

Add this line:
```
*/15 * * * * /home/sara/repos/birdnet-tools/scripts/run_export.sh >> /home/sara/repos/birdnet-tools/export.log 2>&1
```
