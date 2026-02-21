"""Create a backup of the active database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from maint_lib import run_backup


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a Condottiere DB backup.")
    parser.add_argument(
        "--output-dir",
        default="backups",
        help="Backup output directory (default: backups)",
    )
    args = parser.parse_args()

    try:
        result = run_backup(output_dir=Path(args.output_dir))
    except Exception as exc:  # noqa: BLE001
        print("maint-backup", json.dumps({"ok": False, "error": str(exc)}, indent=2))
        sys.exit(1)

    print("maint-backup", json.dumps({"ok": True, **result}, indent=2, sort_keys=True))
