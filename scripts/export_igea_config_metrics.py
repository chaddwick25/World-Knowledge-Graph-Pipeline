#!/usr/bin/env python
"""Generate data/igea_config_metrics.csv from the Django DB.

This is a thin wrapper around the `export_igea_config_metrics` Django
management command, to match the existing scripts/ ergonomics.

Usage (from project root):

    python scripts/export_igea_config_metrics.py

You can pass additional arguments through to the management command, e.g.:

    python scripts/export_igea_config_metrics.py --output data/custom_igea_config_metrics.csv
"""

import os
import sys
from pathlib import Path
from typing import List

# Ensure we can import the backend Django project
ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")

import django  # type: ignore  # noqa: E402
from django.core.management import call_command  # type: ignore  # noqa: E402


def main(argv: List[str]) -> None:
    django.setup()

    # Forward all arguments after the script name directly to the
    # `export_igea_config_metrics` management command so you can
    # override e.g. --output if desired.
    call_command("export_igea_config_metrics", *argv)


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    main(sys.argv[1:])
