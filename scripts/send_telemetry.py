"""Manual telemetry heartbeat entrypoint."""

import argparse
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

from app.telemetry.events import maybe_emit_heartbeat


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send one Condottiere telemetry heartbeat.")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test-send now even if consent is pending/declined or interval has not elapsed.",
    )
    args = parser.parse_args()
    result = maybe_emit_heartbeat(
        force=args.test,
        allow_without_consent=args.test,
        allow_primary_node_emit=args.test,
    )
    print("telemetry", result)
