"""Sender payload formatting tests."""

import pytest

pytest.importorskip("httpx")

from app.delivery.sender import build_discord_payload


def test_discord_payload_uses_resolved_system_and_planet_names():
    notification = {
        "character_name": "Eadrom Vintarus",
        "notification_id": 2326136044,
        "type": "MercenaryDenAttacked",
        "timestamp": "2026-02-18T00:39:00Z",
        "raw_text": (
            "solarsystemID: 30002360\n"
            "planetID: 40150138\n"
            "aggressorCorporationName: <a href=\"showinfo:2//98510288\">KarmaFleet</a>\n"
        ),
    }
    payload = build_discord_payload(
        notification,
        mention_text=None,
        name_lookup={
            30002360: "Aldrat",
            40150138: "Aldrat I",
        },
    )
    content = payload["content"]
    assert "system `Aldrat`" in content
    assert "planet `Aldrat I`" in content
    assert "aggressor `KarmaFleet`" in content
