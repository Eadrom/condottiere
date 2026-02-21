"""Maintenance preflight checks."""

from __future__ import annotations

import json
import sys

from maint_lib import run_preflight


if __name__ == "__main__":
    result = run_preflight()
    print("maint-preflight", json.dumps(result, indent=2, sort_keys=True))
    sys.exit(0 if result.get("ok") else 1)
