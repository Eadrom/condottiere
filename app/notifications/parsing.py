"""Notification text parsing scaffold."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from typing import Any


def _coerce_scalar(value: str) -> Any:
    raw = value.strip()
    if raw == "":
        return ""
    lower = raw.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if re.fullmatch(r"-?\d+", raw):
        try:
            return int(raw)
        except ValueError:
            return raw
    if re.fullmatch(r"-?\d+\.\d+", raw):
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw


def _parse_key_value_lines(raw_text: str) -> dict:
    parsed: dict[str, Any] = {}
    for line in raw_text.splitlines():
        if not line or ":" not in line:
            continue
        if line.startswith("-") or line.startswith(" "):
            # Ignore nested/list YAML-like content in fallback mode.
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if not key:
            continue
        parsed[key] = _coerce_scalar(value)
    return parsed


def _try_parse_yaml(raw_text: str) -> dict:
    try:
        import yaml  # type: ignore
    except Exception:
        return {}

    try:
        payload = yaml.safe_load(raw_text)  # noqa: S506 - safe_load used intentionally.
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_notification_text(raw_text: str) -> dict:
    """Parse raw notification text into best-effort metadata."""
    yaml_parsed = _try_parse_yaml(raw_text)
    if yaml_parsed:
        return yaml_parsed
    return _parse_key_value_lines(raw_text)


# EVE notification timestamps such as timestampEntered/timestampExited are Windows
# FILETIME values: 100ns ticks since 1601-01-01 UTC.
_FILETIME_UNIX_EPOCH = 116444736000000000
_FILETIME_TICKS_PER_SECOND = 10_000_000
# Merc Den reinforcement is ~24h +/- 6h; anything far outside that is bad data.
_MAX_REINFORCEMENT_HOURS = 48


def filetime_to_datetime(value: Any) -> datetime | None:
    """Convert a FILETIME tick count to a naive UTC datetime, or None if invalid."""
    try:
        ticks = int(value)
    except (TypeError, ValueError):
        return None
    if ticks <= _FILETIME_UNIX_EPOCH:
        return None
    seconds = (ticks - _FILETIME_UNIX_EPOCH) / _FILETIME_TICKS_PER_SECOND
    try:
        return datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def reinforcement_exit_time(details: dict) -> datetime | None:
    """Return when a reinforced Merc Den becomes vulnerable again, if the data is sane."""
    entered = filetime_to_datetime(details.get("timestampEntered"))
    exited = filetime_to_datetime(details.get("timestampExited"))
    if entered is None or exited is None:
        return None
    if not entered < exited <= entered + timedelta(hours=_MAX_REINFORCEMENT_HOURS):
        return None
    return exited
