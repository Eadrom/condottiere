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


def test_nearest_planet_picks_the_closest():
    from app.esi.client import nearest_planet

    planets = [
        {"planet_id": 1, "position": {"x": 0, "y": 0, "z": 0}},
        {"planet_id": 2, "position": {"x": 5e11, "y": 0, "z": 0}},
        {"planet_id": 3, "position": {"x": -2e10, "y": 1e9, "z": 0}},
    ]
    assert nearest_planet({"x": -2.0001e10, "y": 1e9, "z": 1e7}, planets)["planet_id"] == 3
    assert nearest_planet({"x": 1, "y": 1, "z": 1}, planets)["planet_id"] == 1
    assert nearest_planet({}, planets) is None
    assert nearest_planet({"x": 0, "y": 0, "z": 0}, []) is None
