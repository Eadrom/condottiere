"""Manual Discord webhook test command."""

import argparse
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

# Load env before importing app modules.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

from app.delivery.sender import post_webhook


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a test message to a Discord webhook.")
    parser.add_argument(
        "--webhook-url",
        default=os.getenv("DISCORD_TEST_WEBHOOK_URL", ""),
        help="Discord webhook URL (defaults to DISCORD_TEST_WEBHOOK_URL env var).",
    )
    parser.add_argument(
        "--message",
        default="Condottiere test webhook message.",
        help="Message content to send.",
    )
    parser.add_argument(
        "--mention",
        default="",
        help="Optional mention prefix, e.g. @everyone or <@&role_id>.",
    )
    args = parser.parse_args()

    webhook_url = args.webhook_url.strip()
    placeholder_values = {"TODO", "UPDATE_ME", "CHANGE_ME"}
    if not webhook_url or webhook_url in placeholder_values:
        print("error: webhook URL is required (set DISCORD_TEST_WEBHOOK_URL or pass --webhook-url)")
        return 2

    content_parts = [args.mention.strip(), args.message.strip()]
    payload = {"content": " ".join(part for part in content_parts if part)}
    ok, error = post_webhook(webhook_url, payload)
    if not ok:
        print(f"webhook test failed: {error}")
        return 1

    print("webhook test sent successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
