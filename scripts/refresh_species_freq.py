#!/usr/bin/env python3
"""Refresh the weekly species frequency cache used by push_events.py.

Queries the BirdNET-Pi metadata model for all species frequencies at the
current week of year and location, then writes .species_freq_cache.json
with a map of species to their frequency score for all species present in
this region (frequency > 0). push_events.py reads this cache to surface
exclusion events only for species genuinely expected in this region,
suppressing zero-frequency species that would never appear here.
"""
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _utils import setup_logging

BIRDNET_DIR = Path.home() / "BirdNET-Pi"
BIRDNET_PYTHON = BIRDNET_DIR / "birdnet" / "bin" / "python3"
SPECIES_SCRIPT = BIRDNET_DIR / "scripts" / "species.py"
REPO_DIR = Path(__file__).resolve().parent.parent
CACHE_FILE = REPO_DIR / ".species_freq_cache.json"

# Maximum possible SF_THRESH in BirdNET-Pi. Cache all species up to this
# value so the cache remains valid regardless of the configured threshold.
SF_THRESH_MAX = 0.05


def get_frequencies() -> dict[str, float]:
    if not BIRDNET_PYTHON.exists():
        raise FileNotFoundError(f"BirdNET-Pi venv not found at {BIRDNET_PYTHON}")
    result = subprocess.run(
        [str(BIRDNET_PYTHON), str(SPECIES_SCRIPT), "--threshold", "0.0"],
        capture_output=True, text=True,
        cwd=str(BIRDNET_DIR / "scripts"),
    )
    if result.returncode != 0:
        raise RuntimeError(f"species.py failed:\n{result.stderr}")
    freqs: dict[str, float] = {}
    for line in result.stdout.splitlines():
        m = re.match(r"^(.+) - ([0-9.]+)$", line.strip())
        if m:
            freqs[m.group(1)] = float(m.group(2))
    return freqs


def main() -> None:
    setup_logging()
    logging.info("Fetching species frequencies from BirdNET metadata model...")
    freqs = get_frequencies()
    freq_filtered = {name: freq for name, freq in freqs.items() if 0 < freq <= SF_THRESH_MAX}
    week = datetime.now().isocalendar()[1]
    logging.info(
        "Total species: %d, nonzero: %d, in band (0, %.2f]: %d (week %d)",
        len(freqs), sum(1 for f in freqs.values() if f > 0), SF_THRESH_MAX, len(freq_filtered), week,
    )
    cache = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "week": week,
        "sf_thresh_max": SF_THRESH_MAX,
        "species_frequencies": freq_filtered,
    }
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    os.replace(tmp, CACHE_FILE)
    logging.info("Cache written to %s", CACHE_FILE)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Unexpected error")
        sys.exit(1)
