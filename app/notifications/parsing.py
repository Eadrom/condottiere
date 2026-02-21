"""Notification text parsing scaffold."""

from __future__ import annotations

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
