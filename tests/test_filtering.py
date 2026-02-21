"""Filtering behavior tests."""

from app.notifications.filtering import is_relevant_notification
from app.notifications.parsing import parse_notification_text


def test_mercenary_den_event_is_relevant():
    assert is_relevant_notification({"type": "MercenaryDenAttacked"})
    assert is_relevant_notification({"type": "MercenaryDenReinforced"})


def test_kill_report_ship_type_filter():
    assert is_relevant_notification(
        {"type": "KillReportVictim", "victimShipTypeID": 85230}
    )
    assert not is_relevant_notification(
        {"type": "KillReportVictim", "victimShipTypeID": 12345}
    )


def test_kill_report_uses_parsed_text_when_ship_type_is_not_top_level():
    raw_text = "killMailHash: abc\nkillMailID: 1\nvictimShipTypeID: 85230\n"
    parsed = parse_notification_text(raw_text)
    assert is_relevant_notification({"type": "KillReportVictim", "text": raw_text}, parsed)
