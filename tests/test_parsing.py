"""Notification text parsing tests."""

from app.notifications.parsing import parse_notification_text


def test_parse_key_values_from_notification_text():
    raw_text = (
        "aggressorCharacterID: 1110653236\n"
        "armorPercentage: 100.0\n"
        "solarsystemID: 30002357\n"
        "victimShipTypeID: 85230\n"
    )
    parsed = parse_notification_text(raw_text)
    assert parsed["aggressorCharacterID"] == 1110653236
    assert parsed["armorPercentage"] == 100.0
    assert parsed["solarsystemID"] == 30002357
    assert parsed["victimShipTypeID"] == 85230


def test_parse_handles_empty_or_invalid_lines():
    raw_text = "foo: bar\nthis is not valid\n- ignored\n nested: ignored\n"
    parsed = parse_notification_text(raw_text)
    assert parsed["foo"] == "bar"
