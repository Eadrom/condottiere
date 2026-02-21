"""Webhook resolution priority tests."""

from app.delivery.resolver import choose_destination


def test_resolution_prefers_corp_when_enabled_and_configured():
    destination = choose_destination(
        character_id=42,
        corporation_id=9001,
        use_corp_webhook=True,
        personal_webhook_url="https://discord.com/api/webhooks/personal",
        personal_mention_text="@here",
        corp_webhook_url="https://discord.com/api/webhooks/corp",
        corp_mention_text=None,
        default_mention="",
        dev_fallback_webhook_url="https://discord.com/api/webhooks/dev",
    )
    assert destination is not None
    assert destination.destination_key == "corp:9001"
    assert destination.webhook_url == "https://discord.com/api/webhooks/corp"
    assert destination.mention_text is None


def test_resolution_falls_back_to_personal():
    destination = choose_destination(
        character_id=42,
        corporation_id=9001,
        use_corp_webhook=True,
        personal_webhook_url="https://discord.com/api/webhooks/personal",
        personal_mention_text="",
        corp_webhook_url=None,
        corp_mention_text=None,
        default_mention="@here",
        dev_fallback_webhook_url="https://discord.com/api/webhooks/dev",
    )
    assert destination is not None
    assert destination.destination_key == "character:42"
    assert destination.webhook_url == "https://discord.com/api/webhooks/personal"
    assert destination.mention_text == "@here"


def test_resolution_uses_dev_fallback_when_no_character_or_corp_webhook():
    destination = choose_destination(
        character_id=42,
        corporation_id=9001,
        use_corp_webhook=False,
        personal_webhook_url=None,
        personal_mention_text="",
        corp_webhook_url=None,
        corp_mention_text=None,
        default_mention="",
        dev_fallback_webhook_url="https://discord.com/api/webhooks/dev",
    )
    assert destination is not None
    assert destination.destination_key == "dev:test-webhook"
    assert destination.webhook_url == "https://discord.com/api/webhooks/dev"


def test_resolution_uses_personal_mention_when_personal_webhook_selected():
    destination = choose_destination(
        character_id=42,
        corporation_id=9001,
        use_corp_webhook=False,
        personal_webhook_url="https://discord.com/api/webhooks/personal",
        personal_mention_text="<@123>",
        corp_webhook_url=None,
        corp_mention_text=None,
        default_mention="",
        dev_fallback_webhook_url=None,
    )
    assert destination is not None
    assert destination.destination_key == "character:42"
    assert destination.mention_text == "<@123>"
