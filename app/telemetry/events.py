"""Opt-in telemetry emitter/collector helpers.

Privacy model:
- Emits only install UUID + monitored character count + version + timestamp.
- Never emits character IDs, corp IDs, refresh tokens, or raw notifications.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import metadata
import json
import uuid

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.config import (
    TELEMETRY_EMIT_INTERVAL_SECONDS,
    is_primary_telemetry_node,
    telemetry_collector_base_url,
)
from app.db.models import AppState, Character
from app.db.session import SessionLocal

_INSTALL_ID_KEY = "telemetry.install_id"
_LAST_SENT_AT_KEY = "telemetry.last_sent_at"
_LAST_ERROR_KEY = "telemetry.last_error"
_CONSENT_KEY = "telemetry.consent"
_CONSENT_GRANTED = "granted"
_CONSENT_DECLINED = "declined"
_CONSENT_UNDECIDED = "undecided"
_REMOTE_PREFIX = "telemetry.remote."
_REMOTE_SUFFIX = ".latest"
_HISTORY_MAX_DAYS = 60
_HISTORY_MAX_SAMPLES = 400


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _isoformat_utc(value: datetime) -> str:
    return _to_utc_naive(value).isoformat() + "Z"


def _parse_iso_utc(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _to_utc_naive(parsed)


def _get_state(db, key: str) -> str | None:
    row = db.get(AppState, key)
    if row is None:
        return None
    return row.value


def _set_state(db, key: str, value: str) -> None:
    row = db.get(AppState, key)
    if row is None:
        db.add(AppState(key=key, value=value))
        return
    row.value = value


def _safe_nonnegative_int(value, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _parse_payload_blob(value: str | None) -> dict:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalized_history(payload: dict) -> list[dict]:
    history_entries: list[dict] = []
    raw_history = payload.get("history")
    if isinstance(raw_history, list):
        for entry in raw_history:
            if not isinstance(entry, dict):
                continue
            received_at = _parse_iso_utc(str(entry.get("received_at", "")).strip())
            if received_at is None:
                continue
            monitored_count = _safe_nonnegative_int(
                entry.get("monitored_character_count"),
                default=0,
            )
            history_entries.append(
                {
                    "received_at": received_at,
                    "monitored_character_count": monitored_count,
                }
            )

    # Backward-compatible fallback for payloads that only stored latest values.
    if not history_entries:
        latest_received = _parse_iso_utc(str(payload.get("received_at", "")).strip())
        if latest_received is not None:
            history_entries.append(
                {
                    "received_at": latest_received,
                    "monitored_character_count": _safe_nonnegative_int(
                        payload.get("monitored_character_count"),
                        default=0,
                    ),
                }
            )

    history_entries.sort(key=lambda item: item["received_at"])
    return history_entries


def _app_version() -> str:
    try:
        return metadata.version("condottiere")
    except metadata.PackageNotFoundError:
        return "0.1.0"


def ensure_install_id(db) -> str:
    """Return stable install UUID, creating it if missing."""
    existing = _get_state(db, _INSTALL_ID_KEY)
    if existing:
        try:
            return str(uuid.UUID(existing))
        except ValueError:
            pass

    install_id = str(uuid.uuid4())
    _set_state(db, _INSTALL_ID_KEY, install_id)
    return install_id


def ensure_local_install_id() -> str | None:
    """Create install UUID on startup so identity is stable early."""
    try:
        with SessionLocal() as db:
            install_id = ensure_install_id(db)
            db.commit()
            return install_id
    except SQLAlchemyError:
        return None


def _normalize_consent(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw == _CONSENT_GRANTED:
        return _CONSENT_GRANTED
    if raw == _CONSENT_DECLINED:
        return _CONSENT_DECLINED
    return _CONSENT_UNDECIDED


def get_telemetry_consent_state() -> str:
    """Return telemetry consent state: undecided, granted, or declined."""
    with SessionLocal() as db:
        consent = _normalize_consent(_get_state(db, _CONSENT_KEY))
        db.commit()
    return consent


def set_telemetry_consent(granted: bool) -> str:
    """Persist telemetry consent choice and return normalized state."""
    new_value = _CONSENT_GRANTED if granted else _CONSENT_DECLINED
    with SessionLocal() as db:
        _set_state(db, _CONSENT_KEY, new_value)
        db.commit()
    return new_value


def _count_monitored_characters(db) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(Character)
            .where(
                Character.is_active.is_(True),
                Character.monitoring_enabled.is_(True),
            )
        ).scalar_one()
    )


def maybe_emit_heartbeat(
    *,
    force: bool = False,
    allow_without_consent: bool = False,
    allow_primary_node_emit: bool = False,
) -> dict:
    """Emit one telemetry heartbeat when enabled and due."""
    collector_url = telemetry_collector_base_url()
    if not collector_url:
        return {"emitted": False, "reason": "missing_collector_url"}
    consent = get_telemetry_consent_state()
    if consent != _CONSENT_GRANTED and not allow_without_consent:
        return {
            "emitted": False,
            "reason": (
                "consent_pending"
                if consent == _CONSENT_UNDECIDED
                else "consent_declined"
            ),
        }
    if is_primary_telemetry_node() and not allow_primary_node_emit:
        return {"emitted": False, "reason": "primary_node_no_emit"}

    now = _utc_now()
    interval_seconds = max(TELEMETRY_EMIT_INTERVAL_SECONDS, 60)

    with SessionLocal() as db:
        install_id = ensure_install_id(db)
        last_sent_at = _parse_iso_utc(_get_state(db, _LAST_SENT_AT_KEY))
        monitored_count = _count_monitored_characters(db)
        db.commit()

    if (
        not force
        and last_sent_at is not None
        and now < last_sent_at + timedelta(seconds=interval_seconds)
    ):
        return {
            "emitted": False,
            "reason": "interval_not_elapsed",
            "install_id": install_id,
            "monitored_character_count": monitored_count,
            "next_emit_after": _isoformat_utc(
                last_sent_at + timedelta(seconds=interval_seconds)
            ),
        }

    payload = {
        "install_id": install_id,
        "version": _app_version(),
        "monitored_character_count": monitored_count,
        "timestamp": _isoformat_utc(now),
    }
    headers = {
        "Accept": "application/json",
        "User-Agent": get_settings().eve_user_agent,
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                collector_url.rstrip("/") + "/telemetry/ingest",
                json=payload,
                headers=headers,
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        with SessionLocal() as db:
            _set_state(db, _LAST_ERROR_KEY, str(exc)[:1000])
            db.commit()
        return {
            "emitted": False,
            "reason": "http_error",
            "install_id": install_id,
            "monitored_character_count": monitored_count,
            "error": str(exc),
        }

    with SessionLocal() as db:
        _set_state(db, _LAST_SENT_AT_KEY, _isoformat_utc(now))
        _set_state(db, _LAST_ERROR_KEY, "")
        db.commit()
    return {
        "emitted": True,
        "install_id": install_id,
        "monitored_character_count": monitored_count,
    }


def record_collector_heartbeat(
    *,
    install_id: str,
    version: str,
    monitored_character_count: int,
    reported_at: str | None,
) -> dict:
    """Store latest heartbeat for one remote install."""
    now = _utc_now()
    normalized_reported_at = _parse_iso_utc(reported_at) or now
    monitored_count = _safe_nonnegative_int(monitored_character_count, default=0)
    key = f"{_REMOTE_PREFIX}{install_id}{_REMOTE_SUFFIX}"

    with SessionLocal() as db:
        existing_payload = _parse_payload_blob(_get_state(db, key))
        history = _normalized_history(existing_payload)
        history.append(
            {
                "received_at": now,
                "monitored_character_count": monitored_count,
            }
        )
        cutoff = now - timedelta(days=_HISTORY_MAX_DAYS)
        history = [entry for entry in history if entry["received_at"] >= cutoff]
        history = history[-_HISTORY_MAX_SAMPLES:]

        payload = {
            "install_id": install_id,
            "version": version[:64],
            "monitored_character_count": monitored_count,
            "reported_at": _isoformat_utc(normalized_reported_at),
            "received_at": _isoformat_utc(now),
            "history": [
                {
                    "received_at": _isoformat_utc(entry["received_at"]),
                    "monitored_character_count": int(entry["monitored_character_count"]),
                }
                for entry in history
            ],
        }
        _set_state(db, key, json.dumps(payload, separators=(",", ":")))
        db.commit()

    return payload


def get_collector_install_rows(
    *,
    window_days: int = 30,
    exclude_install_id: str | None = None,
) -> list[dict]:
    """Return per-install telemetry rows with rolling average."""
    now = _utc_now()
    cutoff = now - timedelta(days=max(window_days, 1))
    rows_payloads: list[dict] = []

    with SessionLocal() as db:
        rows = db.execute(
            select(AppState).where(AppState.key.like(f"{_REMOTE_PREFIX}%{_REMOTE_SUFFIX}"))
        ).scalars().all()

    for row in rows:
        payload = _parse_payload_blob(row.value)
        install_id = str(payload.get("install_id", "")).strip()
        if not install_id:
            continue
        if exclude_install_id and install_id == exclude_install_id:
            continue

        history = _normalized_history(payload)
        latest_received = _parse_iso_utc(str(payload.get("received_at", "")).strip())
        latest_count = _safe_nonnegative_int(payload.get("monitored_character_count"), default=0)
        if history:
            latest_received = history[-1]["received_at"]
            latest_count = int(history[-1]["monitored_character_count"])

        samples_30d = [entry for entry in history if entry["received_at"] >= cutoff]
        if samples_30d:
            avg_30d = (
                sum(int(entry["monitored_character_count"]) for entry in samples_30d)
                / len(samples_30d)
            )
        else:
            avg_30d = 0.0

        rows_payloads.append(
            {
                "install_id": install_id,
                "last_received_at": latest_received,
                "latest_monitored_character_count": latest_count,
                "avg_30d_monitored_character_count": avg_30d,
                "samples_30d": len(samples_30d),
            }
        )

    rows_payloads.sort(
        key=lambda item: item["last_received_at"] or datetime.min,
        reverse=True,
    )
    return rows_payloads


def get_collector_summary(*, window_hours: int = 48, exclude_install_id: str | None = None) -> dict:
    """Summarize stored telemetry heartbeats."""
    now = _utc_now()
    cutoff = now - timedelta(hours=max(window_hours, 1))
    rows_payloads = get_collector_install_rows(
        window_days=30,
        exclude_install_id=exclude_install_id,
    )
    active_rows = [
        row
        for row in rows_payloads
        if row["last_received_at"] is not None and row["last_received_at"] >= cutoff
    ]
    last_received = max(
        (
            row["last_received_at"]
            for row in rows_payloads
            if row["last_received_at"] is not None
        ),
        default=None,
    )
    return {
        "remote_installs_total": len(rows_payloads),
        "remote_installs_active_window": len(active_rows),
        "remote_monitored_total": sum(
            int(row["latest_monitored_character_count"]) for row in rows_payloads
        ),
        "remote_monitored_active_window": sum(
            int(row["latest_monitored_character_count"]) for row in active_rows
        ),
        "remote_monitored_30d_avg_total": round(
            sum(float(row["avg_30d_monitored_character_count"]) for row in rows_payloads),
            2,
        ),
        "window_hours": max(window_hours, 1),
        "last_received_at": _isoformat_utc(last_received) if last_received else "-",
    }


def get_local_telemetry_status() -> dict:
    """Return local telemetry status values for admin/debug UI."""
    with SessionLocal() as db:
        install_id = ensure_install_id(db)
        monitored_count = _count_monitored_characters(db)
        last_sent_at = _get_state(db, _LAST_SENT_AT_KEY) or "-"
        last_error = _get_state(db, _LAST_ERROR_KEY) or "-"
        db.commit()
    return {
        "install_id": install_id,
        "monitored_character_count": monitored_count,
        "last_sent_at": last_sent_at,
        "last_error": last_error,
    }
