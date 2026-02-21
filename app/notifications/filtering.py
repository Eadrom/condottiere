"""Mercenary Den filtering scaffold."""

MERC_DEN_TYPES = {
    "MercenaryDenAttacked",
    "MercenaryDenReinforced",
}
KILL_REPORT_TYPE = "KillReportVictim"
MERC_DEN_SHIP_TYPE_ID = 85230


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_relevant_notification(notification: dict, parsed_text: dict | None = None) -> bool:
    """Return True if notification should be tracked.

    Rules:
    - Include direct Merc Den event types
    - Include KillReportVictim when victimShipTypeID == 85230
    """
    notif_type = notification.get("type")
    if notif_type in MERC_DEN_TYPES:
        return True
    if notif_type == KILL_REPORT_TYPE:
        victim_ship_type = _int_or_none(notification.get("victimShipTypeID"))
        if victim_ship_type is None and parsed_text is not None:
            victim_ship_type = _int_or_none(parsed_text.get("victimShipTypeID"))
        return victim_ship_type == MERC_DEN_SHIP_TYPE_ID
    return False
