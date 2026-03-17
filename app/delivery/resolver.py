"""Delivery destination resolution."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db.models import Character, CorpSetting


@dataclass(frozen=True)
class DeliveryDestination:
    """Resolved outbound target for one notification delivery attempt."""

    destination_type: str
    destination_key: str
    webhook_url: str | None = None
    mention_text: str | None = None


def _normalize_mention(mention_text: str | None, default_mention: str) -> str | None:
    selected = mention_text
    if selected is None or not selected.strip():
        selected = default_mention
    normalized = selected.strip()
    return normalized or None


def choose_destination(
    *,
    character_id: int,
    corporation_id: int,
    use_corp_webhook: bool,
    personal_webhook_url: str | None,
    personal_mention_text: str | None,
    corp_webhook_url: str | None,
    corp_mention_text: str | None,
    default_mention: str,
    dev_fallback_webhook_url: str | None = None,
) -> DeliveryDestination | None:
    """Resolve Discord destination priority without DB access."""
    if use_corp_webhook and corp_webhook_url and corp_webhook_url.strip():
        return DeliveryDestination(
            destination_type="discord",
            destination_key=f"corp:{corporation_id}",
            webhook_url=corp_webhook_url.strip(),
            mention_text=_normalize_mention(corp_mention_text, default_mention),
        )

    if personal_webhook_url and personal_webhook_url.strip():
        return DeliveryDestination(
            destination_type="discord",
            destination_key=f"character:{character_id}",
            webhook_url=personal_webhook_url.strip(),
            mention_text=_normalize_mention(personal_mention_text, default_mention),
        )

    if dev_fallback_webhook_url and dev_fallback_webhook_url.strip():
        return DeliveryDestination(
            destination_type="discord",
            destination_key="dev:test-webhook",
            webhook_url=dev_fallback_webhook_url.strip(),
            mention_text=_normalize_mention(None, default_mention),
        )

    return None


def resolve_destination(
    db: Session,
    *,
    character: Character,
    default_mention: str,
    dev_fallback_webhook_url: str | None = None,
) -> DeliveryDestination | None:
    """Resolve destination for a character at send time."""
    corp_webhook_url = None
    corp_mention_text = None
    if character.use_corp_webhook:
        corp_setting = db.get(CorpSetting, character.corporation_id)
        if corp_setting is not None:
            corp_webhook_url = corp_setting.webhook_url
            corp_mention_text = corp_setting.mention_text

    return choose_destination(
        character_id=character.character_id,
        corporation_id=character.corporation_id,
        use_corp_webhook=bool(character.use_corp_webhook),
        personal_webhook_url=character.personal_webhook_url,
        personal_mention_text=(character.personal_mention_text or None),
        corp_webhook_url=corp_webhook_url,
        corp_mention_text=corp_mention_text,
        default_mention=default_mention,
        dev_fallback_webhook_url=dev_fallback_webhook_url,
    )


def resolve_destination_with_debug(
    db: Session,
    *,
    character: Character,
    default_mention: str,
    dev_fallback_webhook_url: str | None = None,
) -> tuple[DeliveryDestination | None, dict[str, object]]:
    """Resolve destination and include safe debug metadata for logs."""
    corp_setting = None
    corp_webhook_url = None
    corp_mention_text = None
    if character.use_corp_webhook:
        corp_setting = db.get(CorpSetting, character.corporation_id)
        if corp_setting is not None:
            corp_webhook_url = corp_setting.webhook_url
            corp_mention_text = corp_setting.mention_text

    destination = choose_destination(
        character_id=character.character_id,
        corporation_id=character.corporation_id,
        use_corp_webhook=bool(character.use_corp_webhook),
        personal_webhook_url=character.personal_webhook_url,
        personal_mention_text=(character.personal_mention_text or None),
        corp_webhook_url=corp_webhook_url,
        corp_mention_text=corp_mention_text,
        default_mention=default_mention,
        dev_fallback_webhook_url=dev_fallback_webhook_url,
    )
    debug = {
        "character_id": int(character.character_id),
        "corporation_id": int(character.corporation_id),
        "use_corp_webhook": bool(character.use_corp_webhook),
        "has_personal_webhook": bool((character.personal_webhook_url or "").strip()),
        "has_corp_setting": bool(corp_setting is not None),
        "has_corp_webhook": bool((corp_webhook_url or "").strip()),
        "has_dev_webhook": bool((dev_fallback_webhook_url or "").strip()),
        "resolved_destination": destination.destination_key if destination else None,
    }
    return destination, debug
