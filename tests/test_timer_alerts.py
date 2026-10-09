"""Ping policy, reinforcement warnings, and the twice-daily timer summary."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("httpx")

from app.db.models import CorpSetting, DenTimer, Character
from app.delivery.sender import WebhookPostResult
from app.services import sender_worker, timer_alerts
from app.services.timer_alerts import current_summary_slot, send_due_summaries, send_due_warnings, sync_timers
from tests.test_sender_location_filter import CORP_HOOK, PERSONAL_HOOK, Harness, _f

ROLE = "<@&555>"


def _filetime(dt: datetime) -> int:
    return int(dt.replace(tzinfo=UTC).timestamp() * 10_000_000) + 116444736000000000


def _reinforced_text(entered: datetime, exits: datetime, system=101, planet=40000001) -> str:
    return (
        f"solarsystemID: {system}\nplanetID: {planet}\n"
        f"timestampEntered: {_filetime(entered)}\ntimestampExited: {_filetime(exits)}\n"
    )


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.settings = replace(harness.settings, timer_summary_times_utc=((11, 30), (23, 30)))
    return harness


def _set_corp_mention(h, mention):
    with h.Session() as db:
        db.get(CorpSetting, 500).mention_text = mention
        db.commit()


def _poster(h, fail_first=False):
    state = {"fail": fail_first}

    def post(destination, payload):
        if state["fail"]:
            state["fail"] = False
            return WebhookPostResult(ok=False, error="boom")
        h.posts.append((destination.webhook_url, payload["content"]))
        return WebhookPostResult(ok=True)

    return post


def _mailer(h):
    def mail(character, subject, body):
        h.mails.append((character.character_id, subject))
        return True, None

    return mail


def _names(systems, planets):
    return {101: "A1", 202: "B2", 40000001: "A1 I"}


def _add_timer(h, character_id, exits_at, notification_id, system=101):
    with h.Session() as db:
        db.add(DenTimer(character_id=character_id, notification_id=notification_id, solar_system_id=system,
                        planet_id=40000001, exits_at=exits_at, warned_at=None, created_at=exits_at - timedelta(hours=24)))
        db.commit()


def test_event_alerts_never_ping_and_sent_reinforced_creates_a_timer(h):
    h.universe()
    h.set_corp()
    _set_corp_mention(h, ROLE)
    h.add_character(7, use_corp=True)
    exits = h.now + timedelta(hours=20)
    h.queue(7, "solarsystemID: 101\n", notif_type="MercenaryDenAttacked")
    reinforced = h.queue(7, _reinforced_text(h.now, exits), notif_type="MercenaryDenReinforced")
    h.queue(7, "killMailHash: a\nkillMailID: 1\nvictimShipTypeID: 85230\n", notif_type="KillReportVictim")

    sender_worker.run_sender_once()

    assert len(h.posts) == 3
    assert all(ROLE not in content and "@here" not in content for _, content in h.posts)
    with h.Session() as db:
        timers = db.query(DenTimer).all()
    assert [(t.notification_id, t.exits_at.replace(microsecond=0)) for t in timers] == [(reinforced, exits.replace(microsecond=0))]


def test_filtered_reinforced_alert_gets_no_timer(h):
    u = h.universe()
    h.set_corp(_f(u, list_mode="whitelist", places_text="Beta"))
    h.add_character(7, use_corp=True)
    h.queue(7, _reinforced_text(h.now, h.now + timedelta(hours=20), system=101), notif_type="MercenaryDenReinforced")

    sender_worker.run_sender_once()

    with h.Session() as db:
        assert db.query(DenTimer).count() == 0
        assert sync_timers(db, now=h.now) == 0


def test_warning_pings_once_inside_the_window_and_never_after_exit(h):
    h.set_corp()
    _set_corp_mention(h, ROLE)
    h.add_character(7, use_corp=True)
    exits = datetime(2026, 10, 9, 22, 52)
    _add_timer(h, 7, exits, 1)
    _add_timer(h, 7, datetime(2026, 10, 9, 22, 0), 2)  # already out by the time we look
    post, mail = _poster(h), _mailer(h)

    def run(at):
        with h.Session() as db:
            n = send_due_warnings(db, settings=h.settings, now=at, post_discord=post, send_mail=mail, lookup_names=_names)
            db.commit()
            return n

    assert run(exits - timedelta(minutes=31)) == 0
    assert run(exits - timedelta(minutes=29)) == 1
    assert run(exits - timedelta(minutes=10)) == 0
    assert len(h.posts) == 1
    url, content = h.posts[0]
    assert url == CORP_HOOK and content.startswith(ROLE + "\n")
    assert "Merc Den leaving reinforcement" in content and "planet `A1 I`" in content


def test_failed_warning_retries_and_mail_pilots_get_mail(h):
    h.add_character(8, personal_hook=PERSONAL_HOOK)
    h.add_character(9, mail_scope=True)
    exits = datetime(2026, 10, 9, 22, 52)
    _add_timer(h, 8, exits, 1)
    _add_timer(h, 9, exits, 2)
    post, mail = _poster(h, fail_first=True), _mailer(h)
    at = exits - timedelta(minutes=5)  # timer learned late: still inside the window

    with h.Session() as db:
        assert send_due_warnings(db, settings=h.settings, now=at, post_discord=post, send_mail=mail, lookup_names=_names) == 1
        db.commit()
        assert send_due_warnings(db, settings=h.settings, now=at, post_discord=post, send_mail=mail, lookup_names=_names) == 1
        db.commit()
    assert [url for url, _ in h.posts] == [PERSONAL_HOOK]
    assert h.mails == [(9, "Condottiere Alert: Merc Den leaving reinforcement")]


@pytest.mark.parametrize(
    "now, expected",
    [
        (datetime(2026, 10, 9, 11, 29), None),  # last slot 23:30 yesterday, >2h ago
        (datetime(2026, 10, 9, 11, 30), datetime(2026, 10, 9, 11, 30)),
        (datetime(2026, 10, 9, 13, 30), datetime(2026, 10, 9, 11, 30)),
        (datetime(2026, 10, 9, 13, 31), None),
        (datetime(2026, 10, 10, 0, 15), datetime(2026, 10, 9, 23, 30)),  # crosses midnight
    ],
)
def test_summary_slot_window(now, expected):
    assert current_summary_slot(now, ((11, 30), (23, 30))) == expected


def test_summary_once_per_slot_per_destination_without_ping(h):
    h.set_corp()
    _set_corp_mention(h, ROLE)
    h.add_character(7, use_corp=True)
    h.add_character(8, personal_hook=PERSONAL_HOOK)
    h.add_character(9, mail_scope=True)  # mail-only: no summary
    _add_timer(h, 7, datetime(2026, 10, 10, 3, 0), 1)
    _add_timer(h, 7, datetime(2026, 10, 9, 20, 0), 2)
    _add_timer(h, 8, datetime(2026, 10, 9, 18, 0), 3)
    _add_timer(h, 9, datetime(2026, 10, 9, 19, 0), 4)
    _add_timer(h, 7, datetime(2026, 10, 9, 9, 0), 5)  # already out: not listed
    post = _poster(h, fail_first=True)

    def run(at):
        with h.Session() as db:
            n = send_due_summaries(db, settings=h.settings, now=at, post_discord=post, lookup_names=_names)
            db.commit()
            return n

    slot = datetime(2026, 10, 9, 11, 30)
    assert run(slot - timedelta(minutes=1)) == 0
    assert run(slot + timedelta(minutes=1)) == 1  # one of two posts failed
    assert run(slot + timedelta(minutes=3)) == 1  # the failed one retries
    assert run(slot + timedelta(minutes=5)) == 0  # both done for this slot
    assert sorted(url for url, _ in h.posts) == [CORP_HOOK, PERSONAL_HOOK]
    corp = next(content for url, content in h.posts if url == CORP_HOOK)
    assert ROLE not in corp
    assert corp.startswith("**Upcoming Merc Den timers** (2)")
    assert corp.index("10-09 20:00") < corp.index("10-10 03:00")  # soonest first
    assert "09:00" not in corp

    # Next slot posts again, but only where timers are still upcoming (personal's 18:00 is out).
    assert run(datetime(2026, 10, 9, 23, 31)) == 1
    assert h.posts[-1][0] == CORP_HOOK and h.posts[-1][1].startswith("**Upcoming Merc Den timers** (1)")
