"""ESI client with ETag support."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from app.config import get_settings


def _parse_http_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def fetch_notifications(character_id: int, access_token: str, etag: str | None = None) -> dict:
    """Fetch character notifications with conditional request."""
    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": settings.eve_user_agent,
    }
    if etag:
        headers["If-None-Match"] = etag

    url = f"{settings.eve_esi_base_url.rstrip('/')}/characters/{character_id}/notifications/"
    params = {"datasource": settings.eve_esi_datasource}

    with httpx.Client(timeout=20.0) as client:
        response = client.get(url, headers=headers, params=params)

    # 304 is expected for conditional GETs with If-None-Match.
    if response.status_code not in (200, 304):
        response.raise_for_status()
    new_etag = response.headers.get("ETag") or etag
    notifications = response.json() if response.status_code == 200 else []

    return {
        "status": response.status_code,
        "etag": new_etag,
        "notifications": notifications,
        "expires_at": _parse_http_datetime(response.headers.get("Expires")),
        "x_pages": response.headers.get("X-Pages"),
        "rate_limit_group": response.headers.get("X-Ratelimit-Group"),
        "rate_limit_limit": response.headers.get("X-Ratelimit-Limit"),
        "rate_limit_remaining": response.headers.get("X-Ratelimit-Remaining"),
        "rate_limit_reset": response.headers.get("X-Ratelimit-Reset"),
    }


def refresh_access_token(refresh_token: str) -> dict:
    """Get a fresh access token from EVE SSO."""
    settings = get_settings()
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {
        "Accept": "application/json",
        "User-Agent": settings.eve_user_agent,
    }

    with httpx.Client(timeout=20.0) as client:
        response = client.post(
            settings.eve_token_url,
            data=data,
            headers=headers,
            auth=(settings.eve_client_id, settings.eve_client_secret),
        )

    response.raise_for_status()
    return response.json()


def fetch_character_roles(character_id: int, access_token: str) -> list[str]:
    """Fetch corp roles for webhook authorization checks."""
    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": settings.eve_user_agent,
    }
    url = f"{settings.eve_esi_base_url.rstrip('/')}/characters/{character_id}/roles/"
    params = {"datasource": settings.eve_esi_datasource}

    with httpx.Client(timeout=20.0) as client:
        response = client.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json().get("roles", [])


def send_mail(
    *,
    character_id: int,
    access_token: str,
    recipient_character_id: int,
    subject: str,
    body: str,
) -> int:
    """Send one in-game EVE mail and return mail_id."""
    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": settings.eve_user_agent,
    }
    url = f"{settings.eve_esi_base_url.rstrip('/')}/characters/{character_id}/mail/"
    params = {"datasource": settings.eve_esi_datasource}
    payload = {
        "approved_cost": 0,
        "body": body,
        "recipients": [
            {"recipient_id": int(recipient_character_id), "recipient_type": "character"}
        ],
        "subject": subject,
    }

    with httpx.Client(timeout=20.0) as client:
        response = client.post(url, headers=headers, params=params, json=payload)
    response.raise_for_status()
    return int(response.json())
