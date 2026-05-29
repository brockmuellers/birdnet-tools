# birdnet-tools
Tools for displaying and manipulating data from a [BirdNET-Pi](https://github.com/Nachtzuster/BirdNET-Pi).

This repo should be cloned into the Raspberry Pi that's running your BirdNET installation.

## export_data.py

Exports BirdNET-Pi detection data to a JSON file and uploads it to Cloudflare R2 every 15 minutes. The JSON contains all observations from the last 7 days plus all-time per-species observation counts broken down by month and 15-minute time-of-day bucket.

### Prerequisites

- Python 3
- boto3: `pip3 install --user boto3`
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
