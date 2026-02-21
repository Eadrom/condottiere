"""Discord mention helpers."""

from __future__ import annotations

import re


MENTION_NONE = "none"
MENTION_HERE = "here"
MENTION_EVERYONE = "everyone"
MENTION_USER = "user"
MENTION_ROLE = "role"
MENTION_CHANNEL = "channel"

MENTION_MODES = {
    MENTION_NONE,
    MENTION_HERE,
    MENTION_EVERYONE,
    MENTION_USER,
    MENTION_ROLE,
    MENTION_CHANNEL,
}

_USER_RE = re.compile(r"^<@(\d+)>$")
_ROLE_RE = re.compile(r"^<@&(\d+)>$")
_CHANNEL_RE = re.compile(r"^<#(\d+)>$")
_NUMERIC_RE = re.compile(r"^\d+$")


def _validate_id(raw_id: str) -> str:
    cleaned = raw_id.strip()
    if not cleaned:
        raise ValueError("Mention ID is required for this mention type.")
    if not _NUMERIC_RE.fullmatch(cleaned):
        raise ValueError("Mention ID must be numeric.")
    return cleaned


def build_mention_text(mode: str, raw_id: str | None = None) -> str:
    """Build canonical Discord mention text from mode and ID."""
    selected_mode = (mode or MENTION_NONE).strip().lower()
    if selected_mode not in MENTION_MODES:
        raise ValueError("Unsupported mention mode.")

    if selected_mode == MENTION_NONE:
        return ""
    if selected_mode == MENTION_HERE:
        return "@here"
    if selected_mode == MENTION_EVERYONE:
        return "@everyone"

    mention_id = _validate_id(raw_id or "")
    if selected_mode == MENTION_USER:
        return f"<@{mention_id}>"
    if selected_mode == MENTION_ROLE:
        return f"<@&{mention_id}>"
    return f"<#{mention_id}>"


def mention_form_values(mention_text: str | None) -> dict[str, str]:
    """Return form state for mention radio + ID fields."""
    mention = (mention_text or "").strip()
    values = {
        "mode": MENTION_NONE,
        "user_id": "",
        "role_id": "",
        "channel_id": "",
    }
    if not mention:
        return values
    if mention == "@here":
        values["mode"] = MENTION_HERE
        return values
    if mention == "@everyone":
        values["mode"] = MENTION_EVERYONE
        return values

    user_match = _USER_RE.fullmatch(mention)
    if user_match:
        values["mode"] = MENTION_USER
        values["user_id"] = user_match.group(1)
        return values

    role_match = _ROLE_RE.fullmatch(mention)
    if role_match:
        values["mode"] = MENTION_ROLE
        values["role_id"] = role_match.group(1)
        return values

    channel_match = _CHANNEL_RE.fullmatch(mention)
    if channel_match:
        values["mode"] = MENTION_CHANNEL
        values["channel_id"] = channel_match.group(1)
        return values

    # Backward compatibility for older stored formats.
    if mention.startswith("@&") and _NUMERIC_RE.fullmatch(mention[2:]):
        values["mode"] = MENTION_ROLE
        values["role_id"] = mention[2:]
        return values
    if mention.startswith("@") and _NUMERIC_RE.fullmatch(mention[1:]):
        values["mode"] = MENTION_USER
        values["user_id"] = mention[1:]
        return values
    if mention.startswith("#") and _NUMERIC_RE.fullmatch(mention[1:]):
        values["mode"] = MENTION_CHANNEL
        values["channel_id"] = mention[1:]
        return values

    return values
