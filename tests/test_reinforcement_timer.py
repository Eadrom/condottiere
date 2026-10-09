"""Merc Den reinforcement timer tests."""

from datetime import datetime

import pytest

pytest.importorskip("httpx")

from app.delivery.sender import build_discord_payload, build_eve_mail_content
from app.notifications.parsing import (
    filetime_to_datetime,
    parse_notification_text,
    reinforcement_exit_time,
)

# Timestamps in the format of a MercenaryDenReinforced notification (entered 2026-10-08 19:19 UTC).
REINFORCED_TEXT = (
    "itemID: &id001 1000000000001\n"
    "planetID: 40009077\n"
    "solarsystemID: 30000142\n"
    "timestampEntered: 134359607569694750\n"
    "timestampExited: 134360599659694750\n"
    "typeID: 85230\n"
)
EXIT_UNIX = 1791586365


def _notification(raw_text: str) -> dict:
    return {
        "character_name": "Test Pilot",
        "notification_id": 1234567890,
        "type": "MercenaryDenReinforced",
        "timestamp": datetime(2026, 10, 8, 19, 19),
        "raw_text": raw_text,
    }


def test_filetime_conversion_matches_notification_time():
    assert filetime_to_datetime(134359607569694750) == datetime(2026, 10, 8, 19, 19, 16, 969475)


def test_reinforcement_exit_time_from_real_notification():
    exited = reinforcement_exit_time(parse_notification_text(REINFORCED_TEXT))
    assert exited is not None
    assert exited.replace(microsecond=0) == datetime(2026, 10, 9, 22, 52, 45)


@pytest.mark.parametrize(
    "raw_text",
    [
        "solarsystemID: 30000142\n",
        "timestampEntered: 134359607569694750\n",
        "timestampEntered: garbage\ntimestampExited: 134360599659694750\n",
        # exit before entry
        "timestampEntered: 134360599659694750\ntimestampExited: 134359607569694750\n",
        # exit absurdly far in the future (~11 days)
        "timestampEntered: 134359607569694750\ntimestampExited: 134369607569694750\n",
        "timestampEntered: 0\ntimestampExited: -5\n",
    ],
)
def test_bad_or_missing_timestamps_yield_no_timer(raw_text):
    assert reinforcement_exit_time(parse_notification_text(raw_text)) is None
    content = build_discord_payload(_notification(raw_text), mention_text=None)["content"]
    assert "Out of reinforcement" not in content
    assert "MercenaryDenReinforced" in content


def test_discord_payload_shows_eve_time_and_local_timestamps():
    content = build_discord_payload(
        _notification(REINFORCED_TEXT),
        mention_text="<@&123>",
        name_lookup={30000142: "Jita", 40009077: "Jita IV"},
    )["content"]
    assert content.startswith("<@&123>\n")
    assert f"Out of reinforcement: `2026-10-09 22:52` EVE · <t:{EXIT_UNIX}:F> (<t:{EXIT_UNIX}:R>)" in content


def test_eve_mail_shows_eve_time_without_discord_tags():
    _, body = build_eve_mail_content(_notification(REINFORCED_TEXT), "Condottiere Alert")
    assert "Out of reinforcement: `2026-10-09 22:52` EVE" in body
    assert "<t:" not in body
