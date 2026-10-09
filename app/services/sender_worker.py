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
from app.db.models import Character, CorpSetting, Delivery, Notification
from app.db.session import SessionLocal
from app.delivery.resolver import resolve_destination_with_debug
from app.delivery.sender import (
    build_discord_payload,
    build_eve_mail_content,
    post_webhook_detailed,
)
from app.esi.client import (
    fetch_killmail_system_id,
    refresh_access_token,
    resolve_planet_names,
    resolve_universe_names,
    send_mail,
)
from app.notifications.filtering import KILL_REPORT_TYPE
from app.notifications.location_filter import StoredFilterError, evaluate, load_filter
from app.notifications.parsing import parse_notification_text
from app.security.crypto import decrypt_refresh_token, encrypt_refresh_token
from app.services.backoff import compute_backoff_seconds
from app.services.delivery_policy import (
    MAX_DELIVERY_AGE_HOURS,
    notification_is_stale,
    notification_predates_monitoring_window,
)
from app.services.timer_alerts import send_due_summaries, send_due_warnings, sync_timers
from app.services.universe_health import UniverseGate
from app.universe.model import load_universe

SENDER_BATCH_SIZE = 50
UNIVERSE_HOLD_SECONDS = 300


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


def _extract_name_lookup_ids(notification: Notification) -> tuple[set[int], set[int]]:
    details = parse_notification_text(notification.raw_text or "")
    system_ids: set[int] = set()
    planet_ids: set[int] = set()

    try:
        system_id = int(details.get("solarsystemID"))
    except (TypeError, ValueError):
        system_id = 0
    if system_id > 0:
        system_ids.add(system_id)

    try:
        planet_id = int(details.get("planetID"))
    except (TypeError, ValueError):
        planet_id = 0
    if planet_id > 0:
        planet_ids.add(planet_id)

    return system_ids, planet_ids


def _mark_sent(delivery: Delivery, now: datetime) -> None:
    delivery.status = "sent"
    delivery.last_error = None
    delivery.updated_at = now


def _mark_expired(delivery: Delivery, *, now: datetime, reason: str) -> None:
    delivery.status = "expired"
    delivery.last_error = reason[:1000]
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
    name_lookup: dict[int, str] | None = None,
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
    subject, body = build_eve_mail_content(
        alert_data,
        settings.eve_mail_subject_prefix,
        name_lookup=name_lookup,
    )

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


def _mark_filtered(delivery: Delivery, *, now: datetime, reason: str) -> None:
    delivery.status = "filtered"
    delivery.last_error = reason[:1000]
    delivery.updated_at = now


def _hold_for_universe(delivery: Delivery, *, now: datetime, reason: str) -> None:
    """Keep the delivery pending without counting it as a failed send attempt."""
    delivery.status = "pending"
    delivery.next_attempt_at = now + timedelta(seconds=UNIVERSE_HOLD_SECONDS)
    delivery.last_error = reason[:1000]
    delivery.updated_at = now


def _filter_for_destination(db, *, destination, character: Character) -> str:
    """The filter belongs to wherever the alert is going; corp overrides personal."""
    if destination is not None and destination.destination_key.startswith("corp:"):
        corp_setting = db.get(CorpSetting, character.corporation_id)
        return corp_setting.alert_filter if corp_setting is not None else ""
    return character.alert_filter or ""


def _notification_system_id(notification: Notification) -> int | None:
    details = parse_notification_text(notification.raw_text or "")
    try:
        system_id = int(details.get("solarsystemID"))
    except (TypeError, ValueError):
        system_id = 0
    if system_id > 0:
        return system_id

    if notification.type == KILL_REPORT_TYPE:
        killmail_hash = str(details.get("killMailHash") or "").strip()
        try:
            killmail_id = int(details.get("killMailID"))
        except (TypeError, ValueError):
            return None
        if not killmail_hash:
            return None
        try:
            return fetch_killmail_system_id(killmail_id, killmail_hash)
        except httpx.HTTPError as exc:
            print("sender", f"notification_id={notification.notification_id}", f"killmail-lookup-error={exc}")
    return None


def _make_admin_notifier(db, *, settings, token_cache: dict[int, str]):
    def notify(subject: str, body: str) -> tuple[bool, str | None]:
        if not settings.admin_character_ids:
            return False, "no ADMIN_CHARACTER_IDS configured"
        admin = db.get(Character, settings.admin_character_ids[0])
        if admin is None:
            return False, "admin character has never logged in"
        if MAIL_SEND_SCOPE not in _parse_scopes(admin.scopes):
            return False, f"admin character is missing scope {MAIL_SEND_SCOPE}"
        access_token, token_error = _get_access_token(character=admin, token_cache=token_cache)
        if not access_token:
            return False, token_error
        try:
            send_mail(
                character_id=admin.character_id,
                access_token=access_token,
                recipient_character_id=admin.character_id,
                subject=subject[:120],
                body=body,
            )
        except (httpx.HTTPError, ValueError) as exc:
            return False, f"admin mail HTTP error: {exc}"
        return True, None

    return notify


def _apply_location_filter(
    db,
    *,
    delivery: Delivery,
    notification: Notification,
    character: Character,
    destination,
    universe_gate: UniverseGate,
    now: datetime,
) -> bool:
    """Return True if the delivery should proceed to sending; otherwise it has been handled."""
    try:
        alert_filter = load_filter(_filter_for_destination(db, destination=destination, character=character))
    except StoredFilterError as exc:
        print("sender", f"delivery={delivery.id}", f"filter-error={exc}", "action=send_unfiltered")
        return True
    if not alert_filter.active:
        return True

    universe = universe_gate.get()
    if universe is None:
        _hold_for_universe(delivery, now=now, reason=universe_gate.unavailable_reason or "universe data unavailable")
        print("sender", f"delivery={delivery.id}", "status=held", f"reason={delivery.last_error}")
        return False

    decision = evaluate(alert_filter, _notification_system_id(notification), universe)
    if decision.send:
        if decision.reason.startswith("location unknown"):
            print("sender", f"delivery={delivery.id}", f"filter={decision.reason}")
        return True

    _mark_filtered(delivery, now=now, reason=decision.reason)
    print(
        "sender",
        f"delivery={delivery.id}",
        f"character={character.character_id}",
        "status=filtered",
        f"notification_id={notification.notification_id}",
        f"reason={decision.reason}",
    )
    return False


def _throttled_post(destination, payload: dict, *, settings, last_discord_send_at: dict[str, float]):
    min_gap = max(settings.discord_min_seconds_per_destination, 0.0)
    previous_send = last_discord_send_at.get(destination.destination_key)
    if previous_send is not None and min_gap > 0:
        wait_seconds = min_gap - (time.monotonic() - previous_send)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
    result = post_webhook_detailed(destination.webhook_url, payload)
    if result.ok:
        last_discord_send_at[destination.destination_key] = time.monotonic()
    return result


def _lookup_names(system_ids: set[int], planet_ids: set[int]) -> dict[int, str]:
    names: dict[int, str] = {}
    try:
        if system_ids:
            names.update(resolve_universe_names(list(system_ids)))
        if planet_ids:
            names.update(resolve_planet_names(list(planet_ids)))
    except httpx.HTTPError as exc:
        print("sender", f"timer-name-lookup-error={exc}")
    return names


def _run_timer_alerts(db, *, settings, token_cache: dict[int, str], last_discord_send_at: dict[str, float]) -> tuple[int, int]:
    """Create timers from sent Reinforced alerts, then send due warnings and summaries."""

    def post_discord(destination, payload):
        return _throttled_post(
            destination, payload, settings=settings, last_discord_send_at=last_discord_send_at
        )

    def send_mail_to_self(character: Character, subject: str, body: str) -> tuple[bool, str | None]:
        if MAIL_SEND_SCOPE not in _parse_scopes(character.scopes):
            return False, f"mail unavailable: missing scope {MAIL_SEND_SCOPE}"
        access_token, token_error = _get_access_token(character=character, token_cache=token_cache)
        if not access_token:
            return False, token_error
        try:
            send_mail(
                character_id=character.character_id,
                access_token=access_token,
                recipient_character_id=character.character_id,
                subject=subject,
                body=body,
            )
        except (httpx.HTTPError, ValueError) as exc:
            return False, f"mail HTTP error: {exc}"
        return True, None

    now = datetime.now(UTC).replace(tzinfo=None)
    warnings_sent = summaries_sent = 0
    try:
        universe = load_universe(settings.universe_data_dir)  # for filter re-checks; never rebuilt here
    except Exception as exc:  # noqa: BLE001
        print("sender", f"timer-universe-unreadable={exc!r}")
        universe = None
    # Each phase is isolated: a failure in one must not skip the others or undo
    # bookkeeping for posts already made (warnings and summaries commit per post).
    try:
        sync_timers(db, now=now)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - log and keep the sender alive
        db.rollback()
        print("sender", f"timer-sync-error={exc!r}")
    try:
        warnings_sent = send_due_warnings(
            db,
            settings=settings,
            now=now,
            post_discord=post_discord,
            send_mail=send_mail_to_self,
            lookup_names=_lookup_names,
            universe=universe,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print("sender", f"timer-warning-error={exc!r}")
    try:
        summaries_sent = send_due_summaries(
            db,
            settings=settings,
            now=now,
            post_discord=post_discord,
            lookup_names=_lookup_names,
            universe=universe,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print("sender", f"timer-summary-error={exc!r}")
    return warnings_sent, summaries_sent


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
        universe_name_lookup: dict[int, str] = {}
        universe_gate = UniverseGate(
            db,
            data_dir=settings.universe_data_dir,
            user_agent=settings.eve_user_agent,
            now=now,
            notify_admin=_make_admin_notifier(db, settings=settings, token_cache=token_cache),
        )
        filtered = 0
        held = 0

        system_ids: set[int] = set()
        planet_ids: set[int] = set()
        for _, notification, _ in rows:
            notif_system_ids, notif_planet_ids = _extract_name_lookup_ids(notification)
            system_ids.update(notif_system_ids)
            planet_ids.update(notif_planet_ids)

        if system_ids:
            try:
                universe_name_lookup.update(resolve_universe_names(list(system_ids)))
            except httpx.HTTPError as exc:
                print("sender", f"universe-system-names-error={exc}")
        if planet_ids:
            try:
                universe_name_lookup.update(resolve_planet_names(list(planet_ids)))
            except httpx.HTTPError as exc:
                print("sender", f"universe-planet-names-error={exc}")

        for delivery, notification, character in rows:
            processed += 1
            now = datetime.now(UTC).replace(tzinfo=None)
            if notification_predates_monitoring_window(
                notification,
                character=character,
                settings=settings,
            ):
                _mark_expired(
                    delivery,
                    now=now,
                    reason="notification predates monitoring enable window",
                )
                print(
                    "sender",
                    f"delivery={delivery.id}",
                    f"character={character.character_id}",
                    "status=expired",
                    f"notification_id={notification.notification_id}",
                    "reason=predates_monitoring_window",
                )
                try:
                    db.commit()
                except SQLAlchemyError as exc:
                    db.rollback()
                    print("sender", f"delivery={delivery.id}", f"db-commit-error={exc}")
                continue

            if settings.env.lower() == "prod" and notification_is_stale(notification, now=now):
                _mark_expired(
                    delivery,
                    now=now,
                    reason=(
                        f"notification older than {MAX_DELIVERY_AGE_HOURS}h; "
                        "dropping stale delivery"
                    ),
                )
                print(
                    "sender",
                    f"delivery={delivery.id}",
                    f"character={character.character_id}",
                    "status=expired",
                    f"notification_id={notification.notification_id}",
                )
                try:
                    db.commit()
                except SQLAlchemyError as exc:
                    db.rollback()
                    print("sender", f"delivery={delivery.id}", f"db-commit-error={exc}")
                continue

            destination, destination_debug = resolve_destination_with_debug(
                db,
                character=character,
                default_mention=settings.discord_default_mention,
                dev_fallback_webhook_url=(
                    settings.discord_test_webhook_url
                    if settings.env.lower() == "dev"
                    else None
                ),
            )
            if not _apply_location_filter(
                db,
                delivery=delivery,
                notification=notification,
                character=character,
                destination=destination,
                universe_gate=universe_gate,
                now=now,
            ):
                if delivery.status == "filtered":
                    filtered += 1
                else:
                    held += 1
                try:
                    db.commit()
                except SQLAlchemyError as exc:
                    db.rollback()
                    print("sender", f"delivery={delivery.id}", f"db-commit-error={exc}")
                continue

            if destination is None:
                print(
                    "sender",
                    f"delivery={delivery.id}",
                    f"character={character.character_id}",
                    "destination=none",
                    f"use_corp={destination_debug.get('use_corp_webhook')}",
                    f"has_personal={destination_debug.get('has_personal_webhook')}",
                    f"has_corp_setting={destination_debug.get('has_corp_setting')}",
                    f"has_corp_webhook={destination_debug.get('has_corp_webhook')}",
                    f"has_dev_webhook={destination_debug.get('has_dev_webhook')}",
                )

            if destination and destination.destination_type == "discord" and destination.webhook_url:
                min_gap = max(settings.discord_min_seconds_per_destination, 0.0)
                previous_send = last_discord_send_at.get(destination.destination_key)
                if previous_send is not None and min_gap > 0:
                    elapsed = time.monotonic() - previous_send
                    wait_seconds = min_gap - elapsed
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)

                # Event alerts never ping; only timer warnings use the mention.
                payload = build_discord_payload(
                    _notification_context(notification, character),
                    mention_text=None,
                    name_lookup=universe_name_lookup,
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
                        name_lookup=universe_name_lookup,
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

        warnings_sent, summaries_sent = _run_timer_alerts(
            db,
            settings=settings,
            token_cache=token_cache,
            last_discord_send_at=last_discord_send_at,
        )

    print(
        "sender-summary",
        f"processed={processed}",
        f"sent={sent}",
        f"discord_sent={discord_sent}",
        f"mail_sent={mail_sent}",
        f"retried={retried}",
        f"filtered={filtered}",
        f"held={held}",
        f"timer_warnings={warnings_sent}",
        f"timer_summaries={summaries_sent}",
    )
