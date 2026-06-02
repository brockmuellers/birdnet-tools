"""Shared utilities for birdnet-tools scripts."""
import logging
import os
import sys
from pathlib import Path


def setup_logging() -> None:
    """Configure root logger: [timestamp] LEVEL: message on stdout."""
    logging.addLevelName(logging.WARNING, "WARN")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        fmt="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logging.basicConfig(handlers=[handler], level=logging.INFO)


def local_timezone_name() -> str:
    """Return the local IANA timezone name (e.g. 'America/Los_Angeles')."""
    try:
        link = os.readlink("/etc/localtime")
        marker = "zoneinfo/"
        idx = link.find(marker)
        if idx != -1:
            return link[idx + len(marker):]
    except OSError:
        pass
    try:
        return Path("/etc/timezone").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    raise RuntimeError("Could not determine local IANA timezone name")
