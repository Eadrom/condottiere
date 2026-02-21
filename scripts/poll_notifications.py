"""Systemd-invoked poller entrypoint."""

import argparse
from pathlib import Path

from dotenv import load_dotenv

# Load env before importing app modules that initialize settings/DB at import-time.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

from app.services.poller import run_poller_once


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run one Condottiere polling cycle.")
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Bypass local cache/ETag checks and force ESI pull for this run.",
    )
    args = parser.parse_args()
    run_poller_once(force_refresh=args.force_refresh)
