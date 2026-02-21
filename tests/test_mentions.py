"""Discord mention helper tests."""

from app.delivery.mentions import build_mention_text, mention_form_values


def test_build_mention_text_modes():
    assert build_mention_text("none", "") == ""
    assert build_mention_text("here", "") == "@here"
    assert build_mention_text("everyone", "") == "@everyone"
    assert build_mention_text("user", "123") == "<@123>"
    assert build_mention_text("role", "456") == "<@&456>"
    assert build_mention_text("channel", "789") == "<#789>"


def test_build_mention_text_rejects_invalid_ids():
    try:
        build_mention_text("role", "abc")
    except ValueError as exc:
        assert "numeric" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_mention_form_values_roundtrip_and_legacy():
    assert mention_form_values("<@123>") == {
        "mode": "user",
        "user_id": "123",
        "role_id": "",
        "channel_id": "",
    }
    assert mention_form_values("@&456") == {
        "mode": "role",
        "user_id": "",
        "role_id": "456",
        "channel_id": "",
    }
