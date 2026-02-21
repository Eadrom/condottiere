"""SSO scope constants and helpers."""

BASE_SCOPES = ["publicData"]
MAIL_SEND_SCOPE = "esi-mail.send_mail.v1"
NOTIFICATIONS_SCOPE = "esi-characters.read_notifications.v1"
CORP_ROLES_SCOPE = "esi-characters.read_corporation_roles.v1"
MONITORING_SCOPES = [NOTIFICATIONS_SCOPE, MAIL_SEND_SCOPE]
CORP_WEBHOOK_SCOPES = [CORP_ROLES_SCOPE]


def union_scopes(existing: set[str], requested: list[str]) -> list[str]:
    """Return additive scope set.

    Pseudocode:
    - Add requested scopes to existing scopes
    - Persist as deterministic sorted list
    """
    return sorted(existing.union(requested))
