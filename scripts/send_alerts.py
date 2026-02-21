"""Systemd-invoked sender entrypoint."""

from pathlib import Path

from dotenv import load_dotenv

# Load env before importing app modules that initialize settings/DB at import-time.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

from app.services.sender_worker import run_sender_once


if __name__ == "__main__":
    run_sender_once()
