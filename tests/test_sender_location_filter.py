"""End-to-end sender tests for location filters, hold, self-heal, and admin mail."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import io
import json

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

pytest.importorskip("httpx")

from app.config import get_settings
from app.db.base import Base
from app.db.models import AppState, Character, CorpSetting, Delivery, Notification
from app.delivery.sender import WebhookPostResult
from app.notifications.location_filter import build_filter
from app.services import sender_worker, universe_health
from app.universe import sde
from app.universe.model import Universe
from app.universe.sde import PINNED_SDE_BUILD, UniverseUnavailable, build_cache_from_zip, cache_path
from tests.universe_fixture import ZIP_BYTES

CORP_HOOK = "https://discord.com/api/webhooks/corp"
PERSONAL_HOOK = "https://discord.com/api/webhooks/personal"
A1, B2 = 101, 202


def _write_cache(data_dir):
    zip_path = data_dir / "fixture.zip"
    zip_path.write_bytes(ZIP_BYTES)
    payload = build_cache_from_zip(zip_path, build=PINNED_SDE_BUILD)
    zip_path.unlink()
    path = cache_path(data_dir)
    path.write_text(json.dumps(payload))
    return Universe(payload)


class Harness:
    def __init__(self, tmp_path, monkeypatch):
        engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, autoflush=False)
        self.data_dir = tmp_path / "universe"
        self.data_dir.mkdir()
        self.settings = replace(
            get_settings(),
            env="dev",
            discord_test_webhook_url="",
            discord_min_seconds_per_destination=0.0,
            universe_data_dir=str(self.data_dir),
            admin_character_ids=(1,),
        )
        self.posts: list[tuple[str, str]] = []
        self.mails: list[tuple[int, str]] = []
        self.killmail_systems: dict[int, int] = {}
        monkeypatch.setattr(sender_worker, "SessionLocal", self.Session)
        monkeypatch.setattr(sender_worker, "get_settings", lambda: self.settings)
        monkeypatch.setattr(sender_worker, "resolve_universe_names", lambda ids: {})
        monkeypatch.setattr(sender_worker, "resolve_planet_names", lambda ids: {})
        monkeypatch.setattr(
            sender_worker,
            "post_webhook_detailed",
            lambda url, payload: self.posts.append((url, payload["content"])) or WebhookPostResult(ok=True, status_code=204),
        )
        monkeypatch.setattr(sender_worker, "_get_access_token", lambda **kw: ("token", None))
        monkeypatch.setattr(
            sender_worker,
            "send_mail",
            lambda **kw: self.mails.append((kw["recipient_character_id"], kw["subject"])) or 1,
        )
        monkeypatch.setattr(
            sender_worker,
            "fetch_killmail_location",
            lambda killmail_id, killmail_hash: (self.killmail_systems.get(killmail_id), None, None),
        )
        self.now = datetime.now(UTC).replace(tzinfo=None)
        self._next_notification = 1000

    def universe(self) -> Universe:
        return _write_cache(self.data_dir)

    def add_character(self, character_id, *, use_corp=False, personal_hook=None, alert_filter="", mail_scope=False):
        with self.Session() as db:
            db.add(
                Character(
                    character_id=character_id,
                    character_name=f"Pilot {character_id}",
                    corporation_id=500,
                    refresh_token_encrypted="x",
                    scopes="esi-mail.send_mail.v1" if mail_scope else "",
                    monitoring_enabled=True,
                    monitoring_enabled_at=self.now - timedelta(days=1),
                    personal_webhook_url=personal_hook,
                    personal_mention_text="",
                    use_corp_webhook=use_corp,
                    alert_filter=alert_filter,
                    is_active=True,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            db.commit()

    def set_corp(self, alert_filter=""):
        with self.Session() as db:
            db.merge(
                CorpSetting(
                    corporation_id=500,
                    webhook_url=CORP_HOOK,
                    mention_text="",
                    allowed_roles='["Director"]',
                    updated_by_character_id=1,
                    alert_filter=alert_filter,
                    updated_at=self.now,
                )
            )
            db.commit()

    def queue(self, character_id, raw_text, notif_type="MercenaryDenAttacked") -> int:
        self._next_notification += 1
        nid = self._next_notification
        with self.Session() as db:
            db.add(Notification(character_id=character_id, notification_id=nid, type=notif_type, timestamp=self.now, raw_text=raw_text))
            db.add(
                Delivery(
                    character_id=character_id,
                    notification_id=nid,
                    destination_key=f"character:{character_id}",
                    status="pending",
                    attempts=0,
                    next_attempt_at=self.now - timedelta(seconds=1),
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            db.commit()
        return nid

    def delivery(self, nid) -> Delivery:
        with self.Session() as db:
            return db.query(Delivery).filter_by(notification_id=nid).one()

    def make_due(self):
        with self.Session() as db:
            for d in db.query(Delivery).filter_by(status="pending"):
                d.next_attempt_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
            db.commit()

    def shift_state_times(self, delta):
        """Pretend the recorded build attempt / admin mail happened `delta` earlier."""
        with self.Session() as db:
            for row in db.query(AppState):
                row.value = (datetime.fromisoformat(row.value) - delta).isoformat()
            db.commit()


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def _f(universe, **kw):
    kw = {"list_mode": "off", "places_text": "", "range_origin_text": "", "range_jumps_text": "", **kw}
    return build_filter(universe, **kw).to_json()


def _system(sid):
    return f"solarsystemID: {sid}\n"


def test_corp_filter_applies_to_corp_webhook_pilots_and_overrides_personal(h):
    u = h.universe()
    h.set_corp(_f(u, list_mode="whitelist", places_text="Alpha"))
    # Personal filter would block everything in Alpha; it must be ignored for corp-webhook pilots.
    h.add_character(7, use_corp=True, personal_hook=PERSONAL_HOOK, alert_filter=_f(u, list_mode="blacklist", places_text="Alpha"))
    inside = h.queue(7, _system(A1))
    outside = h.queue(7, _system(B2))

    sender_worker.run_sender_once()

    assert [url for url, _ in h.posts] == [CORP_HOOK]
    assert h.delivery(inside).status == "sent"
    filtered = h.delivery(outside)
    assert filtered.status == "filtered"
    assert "not in the whitelist" in filtered.last_error


def test_personal_filter_applies_to_personal_webhook_and_mail_fallback(h):
    u = h.universe()
    h.set_corp(_f(u))  # corp filter off; these pilots do not use it
    in_range = _f(u, range_origin_text="A1", range_jumps_text="3")
    h.add_character(8, personal_hook=PERSONAL_HOOK, alert_filter=in_range)
    h.add_character(9, alert_filter=in_range, mail_scope=True)  # no webhook: EVE mail fallback
    near_hook, far_hook = h.queue(8, _system(A1)), h.queue(8, _system(B2))
    near_mail, far_mail = h.queue(9, _system(A1)), h.queue(9, _system(B2))

    sender_worker.run_sender_once()

    assert [url for url, _ in h.posts] == [PERSONAL_HOOK]
    assert h.mails == [(9, "Condottiere Alert: MercenaryDenAttacked")]
    assert [h.delivery(n).status for n in (near_hook, far_hook, near_mail, far_mail)] == ["sent", "filtered", "sent", "filtered"]


def test_kill_report_location_comes_from_killmail(h):
    u = h.universe()
    h.add_character(8, personal_hook=PERSONAL_HOOK, alert_filter=_f(u, list_mode="whitelist", places_text="Beta"))
    h.killmail_systems = {111: A1, 222: B2}
    text = "killMailHash: abc\nkillMailID: {}\nvictimShipTypeID: 85230\n"
    blocked = h.queue(8, text.format(111), notif_type="KillReportVictim")
    allowed = h.queue(8, text.format(222), notif_type="KillReportVictim")
    unknown = h.queue(8, text.format(333), notif_type="KillReportVictim")  # lookup fails: fail open

    sender_worker.run_sender_once()

    assert [h.delivery(n).status for n in (blocked, allowed, unknown)] == ["filtered", "sent", "sent"]


def test_no_filter_never_touches_universe_data(h, monkeypatch):
    calls = []
    monkeypatch.setattr(universe_health, "ensure_universe_cache", lambda *a, **k: calls.append(1))
    h.add_character(8, personal_hook=PERSONAL_HOOK)
    nid = h.queue(8, _system(B2))

    sender_worker.run_sender_once()

    assert h.delivery(nid).status == "sent"
    assert calls == []


def test_missing_universe_holds_filtered_alerts_mails_admin_daily_and_heals(h, monkeypatch):
    u = _f(h.universe(), list_mode="whitelist", places_text="Alpha")
    cache_path(h.data_dir).unlink()  # deleted externally after setup
    h.add_character(1, mail_scope=True)  # admin, for the warning mail
    h.add_character(8, personal_hook=PERSONAL_HOOK, alert_filter=u)
    h.add_character(10, personal_hook=PERSONAL_HOOK)  # unfiltered pilot keeps getting alerts
    held = h.queue(8, _system(A1))
    unfiltered = h.queue(10, _system(B2))

    attempts = []

    def failing_build(data_dir, **kw):
        attempts.append(1)
        raise UniverseUnavailable("SDE download failed: 503")

    monkeypatch.setattr(universe_health, "ensure_universe_cache", failing_build)

    sender_worker.run_sender_once()
    d = h.delivery(held)
    assert (d.status, d.attempts) == ("pending", 0)
    assert "universe data unavailable" in d.last_error
    assert h.delivery(unfiltered).status == "sent"
    assert len(attempts) == 1
    assert h.mails == [(1, "Condottiere: universe data unavailable")]

    # Every run with a held alert retries the download (once per run), but within 24h
    # there is no second mail.
    for expected_attempts in (2, 3):
        h.make_due()
        sender_worker.run_sender_once()
        assert len(attempts) == expected_attempts and len(h.mails) == 1

    # Nothing waiting to send: no download attempt at all.
    with h.Session() as db:
        db.query(Delivery).filter_by(notification_id=held).one().next_attempt_at = h.now + timedelta(hours=1)
        db.commit()
    sender_worker.run_sender_once()
    assert len(attempts) == 3

    # A day later: one more mail.
    h.shift_state_times(timedelta(hours=24))
    h.make_due()
    sender_worker.run_sender_once()
    assert len(attempts) == 4 and len(h.mails) == 2

    # Download works again: alert sends and the daily mail lock is cleared.
    monkeypatch.setattr(universe_health, "ensure_universe_cache", lambda data_dir, **kw: _write_cache(h.data_dir))
    h.make_due()
    sender_worker.run_sender_once()
    assert h.delivery(held).status == "sent"
    with h.Session() as db:
        assert db.get(AppState, "universe_admin_mailed_at") is None


def test_corrupt_cache_is_rebuilt_instead_of_crashing_the_sender(h, monkeypatch):
    u = _f(h.universe(), list_mode="whitelist", places_text="Alpha")
    cache_path(h.data_dir).write_text("{not json")
    monkeypatch.setattr(universe_health, "ensure_universe_cache", lambda data_dir, **kw: _write_cache(h.data_dir))
    h.add_character(8, personal_hook=PERSONAL_HOOK, alert_filter=u)
    nid = h.queue(8, _system(A1))
    sender_worker.run_sender_once()
    assert h.delivery(nid).status == "sent"


@pytest.mark.parametrize("error", [OSError("No space left on device"), sde.UniverseBusy("being built by another process")])
def test_build_errors_hold_alerts_without_crashing(h, monkeypatch, error):
    u = _f(h.universe(), list_mode="whitelist", places_text="Alpha")
    cache_path(h.data_dir).unlink()
    h.add_character(1, mail_scope=True)
    h.add_character(8, personal_hook=PERSONAL_HOOK, alert_filter=u)
    h.add_character(10, personal_hook=PERSONAL_HOOK)

    def boom(data_dir, **kw):
        raise error

    monkeypatch.setattr(universe_health, "ensure_universe_cache", boom)
    held, unfiltered = h.queue(8, _system(A1)), h.queue(10, _system(B2))
    sender_worker.run_sender_once()
    assert h.delivery(held).status == "pending"
    assert h.delivery(unfiltered).status == "sent"
    busy = isinstance(error, sde.UniverseBusy)
    assert h.mails == ([] if busy else [(1, "Condottiere: universe data unavailable")])


def test_kill_post_shows_system_and_planet(h, monkeypatch):
    monkeypatch.setattr(
        sender_worker,
        "fetch_killmail_location",
        lambda killmail_id, killmail_hash: (A1, 40000001, "A1 I") if killmail_id == 111 else (_ for _ in ()).throw(httpx.ConnectError("esi down")),
    )
    monkeypatch.setattr(sender_worker, "resolve_universe_names", lambda ids: {A1: "A1"})
    h.add_character(8, personal_hook=PERSONAL_HOOK)
    text = "killMailHash: abc\nkillMailID: {}\nvictimShipTypeID: 85230\n"
    found = h.queue(8, text.format(111), notif_type="KillReportVictim")
    lookup_fails = h.queue(8, text.format(222), notif_type="KillReportVictim")

    sender_worker.run_sender_once()

    posts = [content for _, content in h.posts]
    assert len(posts) == 2
    assert "system `A1` | planet `A1 I`" in posts[0]
    assert "KillReportVictim" in posts[1] and "system" not in posts[1]  # sent anyway, just without location
    assert h.delivery(found).status == h.delivery(lookup_fails).status == "sent"
