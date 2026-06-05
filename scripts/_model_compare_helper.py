#!/usr/bin/env python3
"""Helper script run via the BirdNET-Pi venv.

Instantiates both MDataModel1 and MDataModel2 with threshold=0.0, queries
each for all species frequencies at the configured location and current week,
and writes a JSON object {"v1": {species: freq, ...}, "v2": {...}} to stdout.
"""
import datetime
import json
import os
import sys

from utils.helpers import get_settings, MODEL_PATH
from utils.models import MDataModel1, MDataModel2

if __name__ == '__main__':
    conf = get_settings()
    lat = conf.getfloat('LATITUDE')
    lon = conf.getfloat('LONGITUDE')
    week = datetime.datetime.today().isocalendar()[1]

    labels_path = os.path.join(MODEL_PATH, 'labels.txt')
    with open(labels_path) as f:
        labels = [line.strip() for line in f]

    out: dict[str, dict[str, float]] = {}
    for key, ModelClass in [('v1', MDataModel1), ('v2', MDataModel2)]:
        model = ModelClass(0.0)
        model.set_meta_data(lat, lon, week)
        out[key] = {label: float(score) for score, label in model.get_species_list_details(labels)}

    # Write JSON to stdout only; any TF noise goes to stderr and is ignored by the caller.
    sys.stdout.write(json.dumps(out))
