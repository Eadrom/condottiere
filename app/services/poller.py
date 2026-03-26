"""Notification poller service."""

from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import InvalidToken
import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db.models import Character, Delivery, EsiState, Notification
from app.db.session import SessionLocal
from app.esi.client import fetch_notifications, refresh_access_token
from app.notifications.filtering import is_relevant_notification
from app.notifications.parsing import parse_notification_text
from app.security.crypto import decrypt_refresh_token, encrypt_refresh_token
from app.services.delivery_policy import has_delivery_channel, monitoring_enable_cutoff
from app.telemetry.events import maybe_emit_heartbeat

POLLER_TIME_BUDGET_SECONDS = 60


def _parse_esi_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _store_relevant_notifications(
    db,
    *,
    character_id: int,
    notifications: list[dict[str, Any]],
    min_timestamp: datetime | None = None,
) -> tuple[int, int, list[int]]:
    relevant_rows: list[dict[str, Any]] = []
    for notification in notifications:
        parsed_text = parse_notification_text(str(notification.get("text", "")))
        if not is_relevant_notification(notification, parsed_text=parsed_text):
            continue

        notification_id = notification.get("notification_id")
        if notification_id is None:
            continue

        timestamp = _parse_esi_timestamp(notification.get("timestamp"))
        if timestamp is None:
            continue
        if min_timestamp and timestamp < min_timestamp:
            continue

        relevant_rows.append(
            {
                "notification_id": int(notification_id),
                "type": str(notification.get("type", "")),
                "timestamp": timestamp,
                "raw_text": str(notification.get("text", "")),
            }
        )

    if not relevant_rows:
        return 0, 0, []

    relevant_ids = [row["notification_id"] for row in relevant_rows]
    existing_ids = set(
        db.execute(
            select(Notification.notification_id).where(
                Notification.character_id == character_id,
                Notification.notification_id.in_(relevant_ids),
            )
        )
        .scalars()
        .all()
    )

    inserted = 0
    inserted_notification_ids: list[int] = []
    for row in relevant_rows:
        if row["notification_id"] in existing_ids:
            continue
        db.add(
            Notification(
                character_id=character_id,
                notification_id=row["notification_id"],
                type=row["type"],
                timestamp=row["timestamp"],
                raw_text=row["raw_text"],
            )
        )
        inserted += 1
        inserted_notification_ids.append(row["notification_id"])
    return len(relevant_rows), inserted, inserted_notification_ids


def _enqueue_deliveries_for_notifications(
    db,
    *,
    character_id: int,
    notification_ids: list[int],
    now: datetime,
) -> int:
    """Create pending deliveries for newly inserted relevant notifications."""
    if not notification_ids:
        return 0

    destination_key = f"character:{character_id}"
    existing_ids = set(
        db.execute(
            select(Delivery.notification_id).where(
                Delivery.character_id == character_id,
                Delivery.destination_key == destination_key,
                Delivery.notification_id.in_(notification_ids),
            )
        )
        .scalars()
        .all()
    )

    queued = 0
    for notification_id in notification_ids:
        if notification_id in existing_ids:
            continue
        db.add(
            Delivery(
                character_id=character_id,
                notification_id=notification_id,
                destination_key=destination_key,
                status="pending",
                attempts=0,
                next_attempt_at=now,
                last_error=None,
                created_at=now,
                updated_at=now,
            )
        )
        queued += 1
    return queued


def run_poller_once(force_refresh: bool = False) -> None:
    """Run one polling cycle for active monitored characters.

    Current scope:
    - Refresh access token
    - Conditional notifications pull with If-None-Match/ETag
    - Respect ESI cache via Expires
    - Persist ESI poll state and errors
    - Parse and store only relevant Merc Den notifications idempotently
    """
    started_at = datetime.now(UTC).replace(tzinfo=None)
    deadline = compute_next_poll_deadline(started_at, POLLER_TIME_BUDGET_SECONDS)
    effective_force_refresh = force_refresh
    settings = get_settings()

    with SessionLocal() as db:
        characters = (
            db.execute(
                select(Character).where(
                    Character.is_active.is_(True),
                    Character.monitoring_enabled.is_(True),
                    Character.refresh_token_encrypted.is_not(None),
                )
            )
            .scalars()
            .all()
        )

        processed = 0
        skipped_due_cache = 0
        for character in characters:
            now = datetime.now(UTC).replace(tzinfo=None)
            if now >= deadline:
                break

            esi_state = db.get(EsiState, character.character_id)
            if esi_state is None:
                esi_state = EsiState(
                    character_id=character.character_id,
                    notif_etag=None,
                    notif_expires_at=None,
                    last_polled_at=None,
                    last_error=None,
                )
                db.add(esi_state)
                db.flush()

            if (
                not effective_force_refresh
                and esi_state.notif_expires_at
                and now < esi_state.notif_expires_at
            ):
                skipped_due_cache += 1
                continue

            try:
                refresh_token = decrypt_refresh_token(character.refresh_token_encrypted or "")
                token_data = refresh_access_token(refresh_token)
                access_token = token_data["access_token"]

                rotated_refresh = token_data.get("refresh_token")
                if rotated_refresh:
                    character.refresh_token_encrypted = encrypt_refresh_token(rotated_refresh)

                result = fetch_notifications(
                    character_id=character.character_id,
                    access_token=access_token,
                    etag=None if effective_force_refresh else esi_state.notif_etag,
                )
                min_timestamp = (
                    None
                    if effective_force_refresh
                    else monitoring_enable_cutoff(character, settings=settings)
                )
                relevant_count, inserted_count, inserted_notification_ids = _store_relevant_notifications(
                    db,
                    character_id=character.character_id,
                    notifications=result["notifications"],
                    min_timestamp=min_timestamp,
                )
                queued_count = 0
                if inserted_notification_ids and has_delivery_channel(
                    db,
                    character=character,
                    settings=settings,
                ):
                    queued_count = _enqueue_deliveries_for_notifications(
                        db,
                        character_id=character.character_id,
                        notification_ids=inserted_notification_ids,
                        now=now,
                    )

                esi_state.notif_etag = result.get("etag") or esi_state.notif_etag
                esi_state.notif_expires_at = result.get("expires_at") or (
                    now + timedelta(minutes=10)
                )
                esi_state.last_polled_at = now
                esi_state.last_error = None

                processed += 1
                print(
                    "poller",
                    f"character={character.character_id}",
                    f"status={result['status']}",
                    f"notifications={len(result['notifications'])}",
                    f"relevant={relevant_count}",
                    f"inserted={inserted_count}",
                    f"queued={queued_count}",
                    f"etag={'set' if result.get('etag') else 'none'}",
                    f"rate_remaining={result.get('rate_limit_remaining')}",
                )
            except InvalidToken:
                esi_state.last_error = (
                    "token/decrypt error: invalid token "
                    "(FERNET_KEY or SESSION_SECRET mismatch between web and poller process)"
                )
                esi_state.last_polled_at = now
            except (KeyError, ValueError) as exc:
                esi_state.last_error = f"token/decrypt error: {exc}"
                esi_state.last_polled_at = now
            except httpx.HTTPError as exc:
                esi_state.last_error = f"http error: {exc}"
                esi_state.last_polled_at = now

            try:
                db.commit()
            except SQLAlchemyError as exc:
                db.rollback()
                print("poller", f"character={character.character_id}", f"db-commit-error={exc}")

    print(
        "poller-summary",
        f"processed={processed}",
        f"cache_skipped={skipped_due_cache}",
        f"force_refresh={effective_force_refresh}",
        f"deadline={deadline.isoformat()}",
    )
    telemetry_result = maybe_emit_heartbeat(force=False)
    print(
        "telemetry-summary",
        f"emitted={telemetry_result.get('emitted')}",
        f"reason={telemetry_result.get('reason', '-')}",
        f"install_id={telemetry_result.get('install_id', '-')}",
        f"monitored={telemetry_result.get('monitored_character_count', '-')}",
    )


def compute_next_poll_deadline(started_at: datetime, budget_seconds: int) -> datetime:
    """Helper for enforcing per-run time budget."""
    return started_at + timedelta(seconds=budget_seconds)
