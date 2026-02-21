"""One-command admin maintenance workflow.

Flow:
1) maint_preflight
2) maint_backup
3) maint_upgrade
4) maint_db_status
"""

from __future__ import annotations

import json
import sys

from maint_lib import run_update_software


if __name__ == "__main__":
    result = run_update_software()
    print("update-software", json.dumps(result, indent=2, sort_keys=True))
    sys.exit(0 if result.get("ok") else 1)
