"""Discord payload building and webhook send helpers."""

from dataclasses import dataclass
from datetime import datetime
import re

import httpx

from app.notifications.parsing import parse_notification_text


_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class WebhookPostResult:
    ok: bool
    error: str | None = None
    status_code: int | None = None
    retry_after_seconds: float | None = None


def _strip_tags(value: str) -> str:
    return _TAG_RE.sub("", value).strip()


def _format_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value or "unknown-time")


def _as_positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _build_event_summary(notification: dict, name_lookup: dict[int, str] | None = None) -> str:
    notif_type = str(notification.get("type", "MercenaryDenEvent"))
    character_name = str(notification.get("character_name", "Unknown Character"))
    timestamp = _format_timestamp(notification.get("timestamp"))
    notification_id = notification.get("notification_id")
    details = parse_notification_text(str(notification.get("raw_text", "")))

    summary = (
        f"**{notif_type}** for `{character_name}` at `{timestamp}` UTC "
        f"(notification `{notification_id}`)"
    )
    extra: list[str] = []
    solarsystem_id = _as_positive_int(details.get("solarsystemID"))
    if solarsystem_id is not None:
        system_name = (name_lookup or {}).get(solarsystem_id)
        extra.append(f"system `{system_name or solarsystem_id}`")

    planet_id = _as_positive_int(details.get("planetID"))
    if planet_id is not None:
        planet_name = (name_lookup or {}).get(planet_id)
        extra.append(f"planet `{planet_name or planet_id}`")

    aggressor_corp = details.get("aggressorCorporationName")
    if isinstance(aggressor_corp, str):
        aggressor = _strip_tags(aggressor_corp)
        if aggressor:
            extra.append(f"aggressor `{aggressor}`")
    elif details.get("aggressorCharacterID"):
        extra.append(f"aggressor id `{details['aggressorCharacterID']}`")

    if extra:
        summary = f"{summary}\n" + " | ".join(extra)
    return summary


def build_discord_payload(
    notification: dict,
    mention_text: str | None,
    *,
    name_lookup: dict[int, str] | None = None,
) -> dict:
    """Build a concise Discord content payload for one event."""
    parts = []
    if mention_text:
        parts.append(mention_text.strip())
    parts.append(_build_event_summary(notification, name_lookup=name_lookup))
    content = "\n".join(part for part in parts if part).strip()
    if len(content) > 1900:
        content = f"{content[:1897]}..."
    return {"content": content}


def build_eve_mail_content(
    notification: dict,
    subject_prefix: str,
    *,
    name_lookup: dict[int, str] | None = None,
) -> tuple[str, str]:
    """Build (subject, body) for EVE in-game mail fallback."""
    notif_type = str(notification.get("type", "MercenaryDenEvent"))
    character_name = str(notification.get("character_name", "Unknown Character"))
    timestamp = _format_timestamp(notification.get("timestamp"))
    notification_id = notification.get("notification_id")
    summary = _build_event_summary(notification, name_lookup=name_lookup)

    subject = f"{subject_prefix}: {notif_type}"
    body_lines = [
        f"Condottiere alert for {character_name}",
        "",
        f"Event: {notif_type}",
        f"Timestamp (UTC): {timestamp}",
        f"Notification ID: {notification_id}",
        "",
        summary.replace("**", ""),
    ]
    return subject[:120], "\n".join(body_lines).strip()


def _parse_retry_after(response: httpx.Response) -> float | None:
    retry_after_header = response.headers.get("Retry-After")
    if retry_after_header:
        try:
            return max(float(retry_after_header), 0.0)
        except ValueError:
            pass
    try:
        body = response.json()
    except ValueError:
        return None
    retry_after = body.get("retry_after")
    if retry_after is None:
        return None
    try:
        return max(float(retry_after), 0.0)
    except (TypeError, ValueError):
        return None


def post_webhook_detailed(webhook_url: str, payload: dict) -> WebhookPostResult:
    """Send webhook and return structured result for retry behavior."""
    if not webhook_url:
        return WebhookPostResult(ok=False, error="missing webhook url")
    if not isinstance(payload, dict) or "content" not in payload:
        return WebhookPostResult(ok=False, error="invalid payload")

    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(webhook_url, json=payload)
    except httpx.HTTPError as exc:
        return WebhookPostResult(ok=False, error=f"http error: {exc}")

    if 200 <= response.status_code < 300:
        return WebhookPostResult(ok=True, status_code=response.status_code)

    retry_after = _parse_retry_after(response) if response.status_code == 429 else None
    return WebhookPostResult(
        ok=False,
        status_code=response.status_code,
        retry_after_seconds=retry_after,
        error=f"discord webhook returned {response.status_code}: {response.text[:240]}",
    )


def post_webhook(webhook_url: str, payload: dict) -> tuple[bool, str | None]:
    """Backward-compatible tuple result API used by test script."""
    result = post_webhook_detailed(webhook_url, payload)
    return result.ok, result.error
