"""Reinforcement timer warnings and the twice-daily timer summary.

Event alerts (attacked / reinforced / killed) never ping. The ping is reserved for a
warning shortly before a reinforced den becomes vulnerable again, and a summary of
upcoming timers is posted at fixed UTC slots so no timer slips between check-ins.

Timers come from Reinforced alerts that were actually sent. Because a pilot may change
destination, or a filter may change, after that, the destination's filter is checked
again when a warning or summary fires.

Anti-spam rule: every warning and summary is marked done (and committed) BEFORE it is
posted. If saving fails nothing is posted; if posting fails the mark is undone and it
is retried. A ping can be missed in a crash, but never repeated.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
from typing import Callable

from sqlalchemy import and_, select

from app.db.models import AppState, Character, CorpSetting, DenTimer, Delivery, Notification
from app.delivery.resolver import resolve_destination
from app.notifications.location_filter import StoredFilterError, evaluate, load_filter
from app.delivery.sender import (
    WebhookPostResult,
    build_timer_summary_payload,
    build_timer_warning_mail,
    build_timer_warning_payload,
)
from app.notifications.parsing import parse_notification_text, reinforcement_exit_time
from app.universe.model import Universe

REINFORCED_TYPE = "MercenaryDenReinforced"
WARNING_LEAD = timedelta(minutes=30)
TIMER_SYNC_LOOKBACK = timedelta(hours=48)
SUMMARY_GRACE = timedelta(hours=2)
_SUMMARY_KEY_PREFIX = "timer_summary_last_slot:"

PostDiscord = Callable[[object, dict], WebhookPostResult]
SendMail = Callable[[Character, str, str], tuple[bool, str | None]]
LookupNames = Callable[[set[int], set[int]], dict[int, str]]


def _positive_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def sync_timers(db, *, now: datetime) -> int:
    """Create timers for recently sent Reinforced alerts that do not have one yet."""
    rows = db.execute(
        select(Notification)
        .join(
            Delivery,
            and_(
                Delivery.character_id == Notification.character_id,
                Delivery.notification_id == Notification.notification_id,
            ),
        )
        .outerjoin(
            DenTimer,
            and_(
                DenTimer.character_id == Notification.character_id,
                DenTimer.notification_id == Notification.notification_id,
            ),
        )
        .where(
            Notification.type == REINFORCED_TYPE,
            Notification.timestamp >= now - TIMER_SYNC_LOOKBACK,
            Delivery.status == "sent",
            DenTimer.id.is_(None),
        )
    ).scalars().all()

    created = 0
    seen: set[tuple[int, int]] = set()
    for notification in rows:
        # A notification can have more than one sent delivery row; one timer per notification.
        key = (notification.character_id, notification.notification_id)
        if key in seen:
            continue
        seen.add(key)
        details = parse_notification_text(notification.raw_text or "")
        exits_at = reinforcement_exit_time(details)
        if exits_at is None:
            continue
        db.add(
            DenTimer(
                character_id=notification.character_id,
                notification_id=notification.notification_id,
                solar_system_id=_positive_int(details.get("solarsystemID")),
                planet_id=_positive_int(details.get("planetID")),
                exits_at=exits_at,
                warned_at=None,
                created_at=now,
            )
        )
        created += 1
    if created:
        db.flush()
    return created


def _monitored():
    return and_(Character.is_active.is_(True), Character.monitoring_enabled.is_(True))


def _filter_blocks(db, destination, character: Character, system_id: int | None, universe: Universe | None) -> bool:
    """True if the destination's current filter would block an alert in this system."""
    if destination is not None and destination.destination_key.startswith("corp:"):
        corp_setting = db.get(CorpSetting, character.corporation_id)
        raw = corp_setting.alert_filter if corp_setting is not None else ""
    else:
        raw = character.alert_filter or ""
    try:
        alert_filter = load_filter(raw)
    except StoredFilterError:
        return False
    if not alert_filter.active or universe is None:
        # Without map data we cannot re-check; the timer passed the filter when it was created.
        return False
    return not evaluate(alert_filter, system_id, universe).send


def _channel_key(destination) -> str:
    """Stable, non-secret key per webhook URL, so one channel never gets two summaries."""
    return hashlib.sha256(destination.webhook_url.encode()).hexdigest()[:32]


def _timer_context(timer: DenTimer, character: Character) -> dict:
    return {
        "character_name": character.character_name,
        "solar_system_id": timer.solar_system_id,
        "planet_id": timer.planet_id,
        "exits_at": timer.exits_at,
    }


def _names_for(timers: list[DenTimer], lookup_names: LookupNames) -> dict[int, str]:
    systems = {t.solar_system_id for t in timers if t.solar_system_id}
    planets = {t.planet_id for t in timers if t.planet_id}
    if not systems and not planets:
        return {}
    return lookup_names(systems, planets)


def send_due_warnings(
    db,
    *,
    settings,
    now: datetime,
    post_discord: PostDiscord,
    send_mail: SendMail,
    lookup_names: LookupNames,
    universe: Universe | None = None,
) -> int:
    """Warn once per timer when it is within WARNING_LEAD of exiting. Past timers never warn."""
    due = db.execute(
        select(DenTimer, Character)
        .join(Character, Character.character_id == DenTimer.character_id)
        .where(
            DenTimer.warned_at.is_(None),
            DenTimer.exits_at > now,
            DenTimer.exits_at <= now + WARNING_LEAD,
            _monitored(),
        )
        .order_by(DenTimer.exits_at.asc())
    ).all()
    if not due:
        return 0

    names = _names_for([timer for timer, _ in due], lookup_names)
    sent = 0
    for timer, character in due:
        context = _timer_context(timer, character)
        destination = resolve_destination(
            db,
            character=character,
            default_mention=settings.discord_default_mention,
            dev_fallback_webhook_url=(
                settings.discord_test_webhook_url if settings.env.lower() == "dev" else None
            ),
        )

        # Claim first: once this commit succeeds, no later failure can cause a second ping.
        timer.warned_at = now
        db.commit()

        if _filter_blocks(db, destination, character, timer.solar_system_id, universe):
            print("timer-warning", f"timer={timer.id}", f"character={character.character_id}", "status=filtered")
            continue

        try:
            if destination is not None and destination.webhook_url:
                result = post_discord(
                    destination,
                    build_timer_warning_payload(context, destination.mention_text, name_lookup=names),
                )
                ok, error = result.ok, result.error
            elif settings.eve_mail_fallback_enabled:
                subject, body = build_timer_warning_mail(
                    context, settings.eve_mail_subject_prefix, name_lookup=names
                )
                ok, error = send_mail(character, subject, body)
            else:
                ok, error = False, "no destination for timer warning"
        except Exception as exc:  # noqa: BLE001 - treat as not delivered
            db.rollback()
            ok, error = False, repr(exc)

        if not ok:
            timer.warned_at = None  # not delivered: retry next run (still before exit)
        db.commit()  # also persists any refresh token rotated while sending
        print(
            "timer-warning",
            f"timer={timer.id}",
            f"character={character.character_id}",
            f"status={'sent' if ok else 'retry'}",
            f"error={error or '-'}",
        )
        if ok:
            sent += 1
    return sent


def current_summary_slot(now: datetime, slots: tuple[tuple[int, int], ...]) -> datetime | None:
    """The most recent configured slot at or before now, if it is still within the grace window."""
    candidates = []
    for day_offset in (0, -1):
        day = (now + timedelta(days=day_offset)).date()
        for hour, minute in slots:
            slot = datetime(day.year, day.month, day.day, hour, minute)
            if slot <= now:
                candidates.append(slot)
    if not candidates:
        return None
    latest = max(candidates)
    return latest if now - latest <= SUMMARY_GRACE else None


def send_due_summaries(
    db,
    *,
    settings,
    now: datetime,
    post_discord: PostDiscord,
    lookup_names: LookupNames,
    universe: Universe | None = None,
) -> int:
    """Post the upcoming-timer summary once per slot to each Discord channel that has timers."""
    slot = current_summary_slot(now, settings.timer_summary_times_utc)
    if slot is None:
        return 0

    upcoming = db.execute(
        select(DenTimer, Character)
        .join(Character, Character.character_id == DenTimer.character_id)
        .where(DenTimer.exits_at > now, _monitored())
        .order_by(DenTimer.exits_at.asc())
    ).all()
    if not upcoming:
        return 0

    slot_value = slot.isoformat()
    destinations: dict[str, object] = {}
    grouped: dict[str, list[tuple[DenTimer, Character]]] = defaultdict(list)
    for timer, character in upcoming:
        destination = resolve_destination(
            db,
            character=character,
            default_mention="",
            dev_fallback_webhook_url=(
                settings.discord_test_webhook_url if settings.env.lower() == "dev" else None
            ),
        )
        if destination is None or not destination.webhook_url:
            continue  # EVE-mail pilots get warnings, not summaries
        if _filter_blocks(db, destination, character, timer.solar_system_id, universe):
            continue
        key = _channel_key(destination)
        state = db.get(AppState, f"{_SUMMARY_KEY_PREFIX}{key}")
        if state is not None and state.value == slot_value:
            continue  # this channel already has this slot's summary
        grouped[key].append((timer, character))
        destinations[key] = destination

    if not grouped:
        return 0
    names = _names_for([timer for pairs in grouped.values() for timer, _ in pairs], lookup_names)

    posted = 0
    for key, pairs in grouped.items():
        state_key = f"{_SUMMARY_KEY_PREFIX}{key}"
        state = db.get(AppState, state_key)
        previous = state.value if state is not None else None
        # Claim the slot before posting, so a failed save can never cause a repeat summary.
        if state is None:
            state = AppState(key=state_key, value=slot_value)
            db.add(state)
        else:
            state.value = slot_value
        db.commit()

        payload = build_timer_summary_payload(
            [_timer_context(timer, character) for timer, character in pairs],
            name_lookup=names,
        )
        try:
            result = post_discord(destinations[key], payload)
        except Exception as exc:  # noqa: BLE001 - treat as not posted
            db.rollback()
            result = WebhookPostResult(ok=False, error=repr(exc))
        if not result.ok:
            if previous is None:
                db.delete(state)
            else:
                state.value = previous
            db.commit()
        print(
            "timer-summary",
            f"channel={key[:8]}",
            f"slot={slot_value}",
            f"timers={len(pairs)}",
            f"status={'sent' if result.ok else 'retry'}",
            f"error={result.error or '-'}",
        )
        if result.ok:
            posted += 1
    return posted
