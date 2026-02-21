"""Telemetry collector routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from app.telemetry.events import record_collector_heartbeat

router = APIRouter()


@router.post("/ingest")
def ingest_telemetry_heartbeat(payload: dict):
    """Receive one privacy-safe heartbeat from a remote install."""
    install_id_raw = str(payload.get("install_id", "")).strip()
    try:
        install_id = str(uuid.UUID(install_id_raw))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid install_id") from exc

    version = str(payload.get("version", "unknown")).strip() or "unknown"
    monitored = payload.get("monitored_character_count", 0)
    try:
        monitored_count = int(monitored)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="monitored_character_count must be an integer",
        ) from exc
    if monitored_count < 0:
        raise HTTPException(
            status_code=400,
            detail="monitored_character_count must be >= 0",
        )

    reported_at = str(payload.get("timestamp", "")).strip() or None
    saved = record_collector_heartbeat(
        install_id=install_id,
        version=version,
        monitored_character_count=monitored_count,
        reported_at=reported_at,
    )
    return {"status": "ok", "install_id": saved["install_id"]}
