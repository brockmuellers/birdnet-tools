#!/usr/bin/env python3
"""Compare BirdNET species range model V1 vs V2.

Queries both MDataModel1 (V2.4) and MDataModel2 (V2.4 - V2) for all species
frequencies at the configured location and current week, then writes a CSV
sorted by the absolute frequency difference between the two models.
"""
import argparse
import csv
import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _utils import setup_logging

BIRDNET_DIR = Path.home() / "BirdNET-Pi"
BIRDNET_PYTHON = BIRDNET_DIR / "birdnet" / "bin" / "python3"
HELPER_SCRIPT = Path(__file__).resolve().parent / "_model_compare_helper.py"


def fetch_frequencies() -> tuple[dict[str, float], dict[str, float]]:
    if not BIRDNET_PYTHON.exists():
        raise FileNotFoundError(f"BirdNET-Pi venv not found at {BIRDNET_PYTHON}")
    result = subprocess.run(
        [str(BIRDNET_PYTHON), str(HELPER_SCRIPT)],
        capture_output=True, text=True,
        cwd=str(BIRDNET_DIR / "scripts"),
    )
    if result.stderr.strip():
        logging.warning("Helper stderr:\n%s", result.stderr.strip())
    if result.returncode != 0:
        raise RuntimeError(f"Helper script failed (exit {result.returncode})")
    data = json.loads(result.stdout)
    return data["v1"], data["v2"]


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Compare BirdNET range model V1 vs V2, output CSV sorted by frequency distance."
    )
    parser.add_argument("--output", required=True, metavar="FILE", help="Output CSV path.")
    parser.add_argument(
        "--include-zeros", action="store_true",
        help="Include species where both models score 0 (excluded by default).",
    )
    args = parser.parse_args()

    logging.info("Fetching frequencies from both range models...")
    v1, v2 = fetch_frequencies()

    all_species = set(v1) | set(v2)
    logging.info("Total species returned: %d", len(all_species))

    rows = []
    for species in all_species:
        f1 = v1.get(species, 0.0)
        f2 = v2.get(species, 0.0)
        if not args.include_zeros and f1 == 0.0 and f2 == 0.0:
            continue
        rows.append((species, f1, f2, abs(f2 - f1)))

    rows.sort(key=lambda r: r[3], reverse=True)

    output = Path(args.output)
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["species", "freq_v1", "freq_v2", "distance"])
        for species, f1, f2, dist in rows:
            writer.writerow([species, f"{f1:.6f}", f"{f2:.6f}", f"{dist:.6f}"])

    logging.info("Wrote %d rows to %s", len(rows), args.output)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Unexpected error")
        sys.exit(1)
