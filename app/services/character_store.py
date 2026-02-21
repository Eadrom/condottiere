"""Character persistence helpers."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import Character
from app.security.crypto import encrypt_refresh_token

MONITORING_SCOPE = "esi-characters.read_notifications.v1"


def _merge_scopes(existing: str, incoming: list[str]) -> str:
    merged = {scope for scope in existing.split() if scope}
    merged.update(scope for scope in incoming if scope)
    return " ".join(sorted(merged))


def _remove_scope(existing: str, target_scope: str) -> str:
    remaining = [scope for scope in existing.split() if scope and scope != target_scope]
    return " ".join(sorted(set(remaining)))


def upsert_character_from_identity(
    db: Session,
    *,
    character_id: int,
    character_name: str,
    corporation_id: int,
    scopes: list[str],
    enable_monitoring: bool = False,
    refresh_token: str | None = None,
) -> Character:
    """Insert or update a character row from SSO callback identity."""
    now = datetime.now(UTC).replace(tzinfo=None)

    character = db.get(Character, character_id)
    if character is None:
        encrypted_refresh = encrypt_refresh_token(refresh_token) if refresh_token else None
        character = Character(
            character_id=character_id,
            character_name=character_name,
            corporation_id=corporation_id,
            refresh_token_encrypted=encrypted_refresh,
            scopes=_merge_scopes("", scopes),
            monitoring_enabled=enable_monitoring,
            personal_webhook_url=None,
            personal_mention_text="",
            use_corp_webhook=False,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(character)
    else:
        character.character_name = character_name
        character.corporation_id = corporation_id
        character.scopes = _merge_scopes(character.scopes, scopes)
        if refresh_token:
            character.refresh_token_encrypted = encrypt_refresh_token(refresh_token)
        if enable_monitoring:
            character.monitoring_enabled = True
        character.updated_at = now

    db.commit()
    db.refresh(character)
    return character


def disable_character_monitoring(db: Session, *, character_id: int) -> bool:
    """Disable monitoring for a character while preserving account identity."""
    character = db.get(Character, character_id)
    if character is None:
        return False

    now = datetime.now(UTC).replace(tzinfo=None)
    character.monitoring_enabled = False
    character.refresh_token_encrypted = None
    character.scopes = _remove_scope(character.scopes, MONITORING_SCOPE)
    character.updated_at = now
    db.commit()
    return True
