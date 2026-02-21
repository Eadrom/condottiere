"""Delivery queue worker."""

from datetime import UTC, datetime, timedelta
import math
import time

from cryptography.fernet import InvalidToken
import httpx
from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError

from app.auth.scopes import MAIL_SEND_SCOPE
from app.config import get_settings
from app.db.models import Character, Delivery, Notification
from app.db.session import SessionLocal
from app.delivery.resolver import resolve_destination
from app.delivery.sender import (
    build_discord_payload,
    build_eve_mail_content,
    post_webhook_detailed,
)
from app.esi.client import refresh_access_token, send_mail
from app.security.crypto import decrypt_refresh_token, encrypt_refresh_token
from app.services.backoff import compute_backoff_seconds

SENDER_BATCH_SIZE = 50


def _parse_scopes(scopes_blob: str | None) -> set[str]:
    if not scopes_blob:
        return set()
    return {scope for scope in scopes_blob.split() if scope}


def _notification_context(notification: Notification, character: Character) -> dict:
    return {
        "character_id": character.character_id,
        "character_name": character.character_name,
        "corporation_id": character.corporation_id,
        "notification_id": notification.notification_id,
        "type": notification.type,
        "timestamp": notification.timestamp,
        "raw_text": notification.raw_text,
    }


def _mark_sent(delivery: Delivery, now: datetime) -> None:
    delivery.status = "sent"
    delivery.last_error = None
    delivery.updated_at = now


def _schedule_retry(
    delivery: Delivery,
    *,
    now: datetime,
    error: str,
    retry_after_seconds: float | None = None,
) -> None:
    delivery.status = "pending"
    delivery.attempts += 1
    if retry_after_seconds is None:
        delay_seconds = compute_backoff_seconds(delivery.attempts)
    else:
        delay_seconds = max(int(math.ceil(retry_after_seconds)), 1)
    delivery.next_attempt_at = now + timedelta(seconds=delay_seconds)
    delivery.last_error = error[:1000]
    delivery.updated_at = now


def _get_access_token(
    *,
    character: Character,
    token_cache: dict[int, str],
) -> tuple[str | None, str | None]:
    cached = token_cache.get(character.character_id)
    if cached:
        return cached, None

    encrypted_refresh = (character.refresh_token_encrypted or "").strip()
    if not encrypted_refresh:
        return None, "mail fallback unavailable: missing refresh token"

    try:
        refresh_token = decrypt_refresh_token(encrypted_refresh)
    except InvalidToken:
        return (
            None,
            "mail fallback unavailable: invalid encrypted token "
            "(FERNET_KEY or SESSION_SECRET mismatch)",
        )

    try:
        token_data = refresh_access_token(refresh_token)
    except httpx.HTTPError as exc:
        return None, f"mail fallback token refresh HTTP error: {exc}"

    access_token = str(token_data.get("access_token", "")).strip()
    if not access_token:
        return None, "mail fallback token refresh missing access token"

    rotated_refresh = str(token_data.get("refresh_token", "")).strip()
    if rotated_refresh:
        character.refresh_token_encrypted = encrypt_refresh_token(rotated_refresh)

    token_cache[character.character_id] = access_token
    return access_token, None


def _send_eve_mail_fallback(
    *,
    character: Character,
    notification: Notification,
    token_cache: dict[int, str],
) -> tuple[bool, str | None]:
    scopes = _parse_scopes(character.scopes)
    if MAIL_SEND_SCOPE not in scopes:
        return False, f"mail fallback unavailable: missing scope {MAIL_SEND_SCOPE}"

    access_token, token_error = _get_access_token(
        character=character,
        token_cache=token_cache,
    )
    if not access_token:
        return False, token_error or "mail fallback token error"

    settings = get_settings()
    alert_data = _notification_context(notification, character)
    subject, body = build_eve_mail_content(alert_data, settings.eve_mail_subject_prefix)

    try:
        send_mail(
            character_id=character.character_id,
            access_token=access_token,
            recipient_character_id=character.character_id,
            subject=subject,
            body=body,
        )
    except (httpx.HTTPError, ValueError) as exc:
        return False, f"mail fallback HTTP error: {exc}"

    return True, None


def run_sender_once() -> None:
    """Process due deliveries in timestamp order."""
    settings = get_settings()
    now = datetime.now(UTC).replace(tzinfo=None)

    with SessionLocal() as db:
        rows = db.execute(
            select(Delivery, Notification, Character)
            .join(
                Notification,
                and_(
                    Notification.character_id == Delivery.character_id,
                    Notification.notification_id == Delivery.notification_id,
                ),
            )
            .join(Character, Character.character_id == Delivery.character_id)
            .where(
                Delivery.status == "pending",
                Delivery.next_attempt_at <= now,
            )
            .order_by(Notification.timestamp.asc(), Delivery.id.asc())
            .limit(SENDER_BATCH_SIZE)
        ).all()

        processed = 0
        sent = 0
        retried = 0
        discord_sent = 0
        mail_sent = 0
        token_cache: dict[int, str] = {}
        last_discord_send_at: dict[str, float] = {}

        for delivery, notification, character in rows:
            processed += 1
            now = datetime.now(UTC).replace(tzinfo=None)
            destination = resolve_destination(
                db,
                character=character,
                default_mention=settings.discord_default_mention,
                dev_fallback_webhook_url=(
                    settings.discord_test_webhook_url
                    if settings.env.lower() == "dev"
                    else None
                ),
            )

            if destination and destination.destination_type == "discord" and destination.webhook_url:
                min_gap = max(settings.discord_min_seconds_per_destination, 0.0)
                previous_send = last_discord_send_at.get(destination.destination_key)
                if previous_send is not None and min_gap > 0:
                    elapsed = time.monotonic() - previous_send
                    wait_seconds = min_gap - elapsed
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)

                payload = build_discord_payload(
                    _notification_context(notification, character),
                    mention_text=destination.mention_text,
                )
                result = post_webhook_detailed(destination.webhook_url, payload)
                if result.ok:
                    _mark_sent(delivery, now)
                    sent += 1
                    discord_sent += 1
                    last_discord_send_at[destination.destination_key] = time.monotonic()
                    print(
                        "sender",
                        f"delivery={delivery.id}",
                        f"character={character.character_id}",
                        "channel=discord",
                        "status=sent",
                    )
                else:
                    retry_after = result.retry_after_seconds if result.status_code == 429 else None
                    _schedule_retry(
                        delivery,
                        now=now,
                        error=result.error or "discord webhook send failed",
                        retry_after_seconds=retry_after,
                    )
                    retried += 1
                    print(
                        "sender",
                        f"delivery={delivery.id}",
                        f"character={character.character_id}",
                        "channel=discord",
                        "status=retry",
                        f"error={delivery.last_error}",
                    )
            else:
                if not settings.eve_mail_fallback_enabled:
                    _schedule_retry(
                        delivery,
                        now=now,
                        error="no webhook destination and EVE mail fallback is disabled",
                    )
                    retried += 1
                    print(
                        "sender",
                        f"delivery={delivery.id}",
                        f"character={character.character_id}",
                        "channel=eve_mail",
                        "status=retry",
                        f"error={delivery.last_error}",
                    )
                else:
                    ok, error = _send_eve_mail_fallback(
                        character=character,
                        notification=notification,
                        token_cache=token_cache,
                    )
                    if ok:
                        _mark_sent(delivery, now)
                        sent += 1
                        mail_sent += 1
                        print(
                            "sender",
                            f"delivery={delivery.id}",
                            f"character={character.character_id}",
                            "channel=eve_mail",
                            "status=sent",
                        )
                    else:
                        _schedule_retry(
                            delivery,
                            now=now,
                            error=error or "mail fallback send failed",
                        )
                        retried += 1
                        print(
                            "sender",
                            f"delivery={delivery.id}",
                            f"character={character.character_id}",
                            "channel=eve_mail",
                            "status=retry",
                            f"error={delivery.last_error}",
                        )

            try:
                db.commit()
            except SQLAlchemyError as exc:
                db.rollback()
                print("sender", f"delivery={delivery.id}", f"db-commit-error={exc}")

    print(
        "sender-summary",
        f"processed={processed}",
        f"sent={sent}",
        f"discord_sent={discord_sent}",
        f"mail_sent={mail_sent}",
        f"retried={retried}",
    )
