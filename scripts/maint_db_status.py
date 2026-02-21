"""Show current database migration revision state."""

from __future__ import annotations

import json
import sys

from maint_lib import run_db_status


if __name__ == "__main__":
    try:
        result = run_db_status()
    except Exception as exc:  # noqa: BLE001
        print("maint-db-status", json.dumps({"ok": False, "error": str(exc)}, indent=2))
        sys.exit(1)

    print("maint-db-status", json.dumps({"ok": True, **result}, indent=2, sort_keys=True))
