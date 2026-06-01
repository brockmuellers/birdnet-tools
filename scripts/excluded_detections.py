#!/usr/bin/env python3
"""Show detections excluded by the species frequency threshold."""
import argparse
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BIRDNET_DIR = Path.home() / "BirdNET-Pi"
BIRDNET_PYTHON = BIRDNET_DIR / "birdnet" / "bin" / "python3"
SPECIES_SCRIPT = BIRDNET_DIR / "scripts" / "species.py"

EXCLUSION_MARKER = "Excluded as below Species Occurrence Frequency Threshold: "


def get_exclusions(days: int) -> list[tuple[datetime, str, str]]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    result = subprocess.run(
        ["journalctl", "-u", "birdnet_analysis", "--since", since,
         "--output=json", "--no-pager"],
        capture_output=True, text=True,
    )
    exclusions = []
    for line in result.stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get("MESSAGE", "")
        if EXCLUSION_MARKER not in msg:
            continue
        ts = datetime.fromtimestamp(
            int(obj["__REALTIME_TIMESTAMP"]) / 1e6, tz=timezone.utc
        ).astimezone()
        idx = msg.index(EXCLUSION_MARKER) + len(EXCLUSION_MARKER)
        words = msg[idx:].strip().split()
        if len(words) < 3:
            continue
        sci_name = " ".join(words[:2])
        com_name = " ".join(words[2:])
        exclusions.append((ts, sci_name, com_name))
    return exclusions


def get_frequencies() -> dict[str, float]:
    if not BIRDNET_PYTHON.exists():
        print(f"ERROR: BirdNET-Pi venv not found at {BIRDNET_PYTHON}", file=sys.stderr)
        sys.exit(1)
    result = subprocess.run(
        [str(BIRDNET_PYTHON), str(SPECIES_SCRIPT), "--threshold", "0.0"],
        capture_output=True, text=True,
        cwd=str(BIRDNET_DIR / "scripts"),
    )
    if result.returncode != 0:
        print(f"ERROR: species.py failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    freqs = {}
    for line in result.stdout.splitlines():
        m = re.match(r"^(.+) - ([0-9.]+)$", line.strip())
        if m:
            freqs[m.group(1)] = float(m.group(2))
    return freqs


def main():
    parser = argparse.ArgumentParser(
        description="Show detections excluded by the species frequency threshold."
    )
    parser.add_argument("--days", type=int, default=7,
                        help="How many days back to search (default: 7)")
    args = parser.parse_args()

    exclusions = get_exclusions(args.days)
    if not exclusions:
        print(f"No excluded detections found in the last {args.days} days.")
        return

    # Warn if detections span more than 2 ISO (year, week) pairs. The metadata
    # model scores species by week-of-year to reflect seasonal patterns, so
    # frequencies shown (from the current week) may not match what was applied
    # to detections many weeks ago. Adjacent-week drift is usually small, but
    # can matter for strongly migratory species near their arrival/departure window.
    iso_year_weeks = sorted({ts.isocalendar()[:2] for ts, _, _ in exclusions})
    if len(iso_year_weeks) > 2:
        current_week = datetime.now().isocalendar()[1]
        oldest = min(ts for ts, _, _ in exclusions)
        weeks_ago = (datetime.now(timezone.utc) - oldest).days // 7
        week_nums = ", ".join(str(w) for _, w in iso_year_weeks)
        print(
            f"WARN: Detections span {len(iso_year_weeks)} ISO weeks (weeks {week_nums}). "
            f"Frequency values shown are from the current week (week {current_week}) and "
            f"may not reflect what was applied to detections from {weeks_ago}+ weeks ago. "
            f"The metadata model scores each species per week-of-year based on eBird checklist "
            f"data, so values shift gradually with the season. Adjacent-week differences are "
            f"usually negligible, but for strongly migratory species near their arrival or "
            f"departure window the threshold value could differ meaningfully for older detections.",
            file=sys.stderr,
        )
        print(file=sys.stderr)

    print("Fetching species frequencies (this may take a moment)...", file=sys.stderr)
    freqs = get_frequencies()

    ts_w, sci_w, com_w, freq_w = 21, 25, 32, 9
    header = (
        f"{'Timestamp':<{ts_w}}  {'Scientific Name':<{sci_w}}  "
        f"{'Common Name':<{com_w}}  {'Frequency':>{freq_w}}"
    )
    print(f"\nExcluded detections — last {args.days} days ({len(exclusions)} total)\n")
    print(header)
    print("-" * len(header))
    for ts, sci_name, com_name in sorted(exclusions, key=lambda x: x[0], reverse=True):
        freq = freqs.get(f"{sci_name}_{com_name}", math.nan)
        freq_str = f"{freq:.4f}" if not math.isnan(freq) else "n/a"
        print(
            f"{ts.strftime('%Y-%m-%d %H:%M:%S'):<{ts_w}}  "
            f"{sci_name:<{sci_w}}  {com_name:<{com_w}}  {freq_str:>{freq_w}}"
        )


if __name__ == "__main__":
    main()
