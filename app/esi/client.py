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


def resolve_universe_names(entity_ids: list[int]) -> dict[int, str]:
    """Resolve universe IDs (e.g. solar systems, planets) to names."""
    unique_ids: list[int] = []
    seen: set[int] = set()
    for value in entity_ids:
        try:
            entity_id = int(value)
        except (TypeError, ValueError):
            continue
        if entity_id <= 0 or entity_id in seen:
            continue
        seen.add(entity_id)
        unique_ids.append(entity_id)

    if not unique_ids:
        return {}

    settings = get_settings()
    headers = {
        "Accept": "application/json",
        "User-Agent": settings.eve_user_agent,
    }
    params = {"datasource": settings.eve_esi_datasource}
    url = f"{settings.eve_esi_base_url.rstrip('/')}/universe/names/"

    with httpx.Client(timeout=20.0) as client:
        response = client.post(url, headers=headers, params=params, json=unique_ids)
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, list):
        return {}

    names: dict[int, str] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        try:
            entity_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        names[entity_id] = name
    return names


def resolve_planet_names(planet_ids: list[int]) -> dict[int, str]:
    """Resolve planet IDs to names via per-planet endpoint."""
    settings = get_settings()
    unique_ids: list[int] = []
    seen: set[int] = set()
    for value in planet_ids:
        try:
            planet_id = int(value)
        except (TypeError, ValueError):
            continue
        if planet_id <= 0 or planet_id in seen:
            continue
        seen.add(planet_id)
        unique_ids.append(planet_id)

    if not unique_ids:
        return {}

    headers = {
        "Accept": "application/json",
        "User-Agent": settings.eve_user_agent,
    }
    params = {"datasource": settings.eve_esi_datasource}
    base = settings.eve_esi_base_url.rstrip("/")

    names: dict[int, str] = {}
    with httpx.Client(timeout=20.0) as client:
        for planet_id in unique_ids:
            response = client.get(
                f"{base}/universe/planets/{planet_id}/",
                headers=headers,
                params=params,
            )
            # Planet lookup is best-effort. Some IDs may not resolve in ESI.
            if response.status_code == 404:
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                continue
            name = str(payload.get("name", "")).strip()
            if not name:
                continue
            names[planet_id] = name
    return names


def nearest_planet(position: dict, planets: list[dict]) -> dict | None:
    """Pick the planet closest to a position (all in the same system's coordinates)."""
    try:
        x, y, z = float(position["x"]), float(position["y"]), float(position["z"])
    except (KeyError, TypeError, ValueError):
        return None
    best = None
    best_distance = None
    for planet in planets:
        try:
            px, py, pz = (float(planet["position"][axis]) for axis in ("x", "y", "z"))
        except (KeyError, TypeError, ValueError):
            continue
        distance = (x - px) ** 2 + (y - py) ** 2 + (z - pz) ** 2
        if best_distance is None or distance < best_distance:
            best, best_distance = planet, distance
    return best


def fetch_killmail_location(killmail_id: int, killmail_hash: str) -> tuple[int | None, int | None, str | None]:
    """Return (system_id, planet_id, planet_name) for a killmail (public endpoints, no token).

    Killmails carry the system and the victim's position but no planet. A Merc Den sits
    within ~20,000 km of its planet while other planets are millions of km away, so the
    nearest planet identifies it.
    """
    settings = get_settings()
    headers = {
        "Accept": "application/json",
        "User-Agent": settings.eve_user_agent,
    }
    params = {"datasource": settings.eve_esi_datasource}
    base = settings.eve_esi_base_url.rstrip("/")

    with httpx.Client(timeout=20.0) as client:
        response = client.get(
            f"{base}/killmails/{int(killmail_id)}/{killmail_hash}/", headers=headers, params=params
        )
        response.raise_for_status()
        killmail = response.json()
        try:
            system_id = int(killmail["solar_system_id"])
        except (KeyError, TypeError, ValueError):
            return None, None, None
        position = (killmail.get("victim") or {}).get("position")
        if not position:
            return system_id, None, None

        response = client.get(f"{base}/universe/systems/{system_id}/", headers=headers, params=params)
        response.raise_for_status()
        planets = []
        for entry in response.json().get("planets", []) or []:
            planet_id = entry.get("planet_id")
            if not planet_id:
                continue
            planet_response = client.get(
                f"{base}/universe/planets/{int(planet_id)}/", headers=headers, params=params
            )
            if planet_response.status_code == 404:
                continue
            planet_response.raise_for_status()
            planets.append(planet_response.json())

    planet = nearest_planet(position, planets)
    if planet is None:
        return system_id, None, None
    return system_id, int(planet["planet_id"]), str(planet.get("name") or "").strip() or None
