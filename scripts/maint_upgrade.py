"""Run Alembic upgrade to head."""

from __future__ import annotations

import argparse
import json
import sys

from maint_lib import run_upgrade


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply Condottiere DB migrations.")
    parser.add_argument(
        "--no-auto-stamp",
        action="store_true",
        help="Do not auto-stamp existing unmanaged schemas.",
    )
    args = parser.parse_args()

    try:
        result = run_upgrade(auto_stamp_existing=not args.no_auto_stamp)
    except Exception as exc:  # noqa: BLE001
        print("maint-upgrade", json.dumps({"ok": False, "error": str(exc)}, indent=2))
        sys.exit(1)

    print("maint-upgrade", json.dumps({"ok": True, **result}, indent=2, sort_keys=True))
