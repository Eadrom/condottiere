"""CSRF token helpers for server-rendered form POST actions."""

from __future__ import annotations

import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_settings

_SESSION_KEY = "_csrf_session_id"
_CSRF_SALT = "condottiere-csrf-v1"
_CSRF_MAX_AGE_SECONDS = 60 * 60 * 8


def _serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    return URLSafeTimedSerializer(settings.csrf_secret)


def ensure_csrf_session_id(session: dict) -> str:
    """Return stable per-session CSRF binding id, creating one if needed."""
    current = session.get(_SESSION_KEY)
    if isinstance(current, str) and current:
        return current
    new_value = secrets.token_urlsafe(32)
    session[_SESSION_KEY] = new_value
    return new_value


def issue_csrf_token(session_id: str) -> str:
    """Create signed CSRF token bound to a session id."""
    return _serializer().dumps({"sid": session_id}, salt=_CSRF_SALT)


def validate_csrf_token(session_id: str, csrf_token: str) -> bool:
    """Check signature, expiration, and session binding."""
    token = (csrf_token or "").strip()
    if not token:
        return False
    try:
        payload = _serializer().loads(
            token,
            salt=_CSRF_SALT,
            max_age=_CSRF_MAX_AGE_SECONDS,
        )
    except (BadSignature, SignatureExpired):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("sid") == session_id
