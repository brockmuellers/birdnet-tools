#!/usr/bin/env python3
"""Refresh the weekly species frequency cache used by push_events.py.

Queries the BirdNET-Pi metadata model for all species frequencies at the
current week of year and location, then writes .species_freq_cache.json
with the set of species that have a non-zero frequency score.

push_events.py reads this cache to suppress exclusion events for species
that would never appear in this region (frequency == 0.0).
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
    nonzero = sorted(name for name, freq in freqs.items() if freq > 0)
    week = datetime.now().isocalendar()[1]
    logging.info(
        "Total species: %d, nonzero frequency: %d (week %d)",
        len(freqs), len(nonzero), week,
    )
    cache = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "week": week,
        "nonzero_species": nonzero,
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
