# Condottiere

Mercenary Den monitoring and alerting service for EVE Online.

## What This Project Does

- Authenticates pilots with EVE SSO.
- Pulls character notifications with ETag-aware polling.
- Filters and stores relevant Mercenary Den notifications.
- Queues and sends alerts to Discord webhooks with EVE mail fallback.
- Provides web UI pages for login, delivery settings, and admin operations.

## Local Run

1. Create local config and set required values:
   - `cp .env.example .env`
   - Set `EVE_CLIENT_ID`, `EVE_CLIENT_SECRET`, `EVE_REDIRECT_BASE`, `SESSION_SECRET`, `CSRF_SECRET`, `FERNET_KEY`, `ADMIN_CHARACTER_IDS`
2. Install dependencies:
   - `pip install -e .`
3. Start web app:
   - `uvicorn app.main:app --reload --env-file .env`
4. Open `http://localhost:8000` and sign in with EVE SSO.

## Common Commands

- One poll cycle:
  - `python scripts/poll_notifications.py`
- Force a hard poll (testing):
  - `python scripts/poll_notifications.py --force-refresh`
- One sender cycle:
  - `python scripts/send_alerts.py`
- One telemetry test emit:
  - `python scripts/send_telemetry.py --test`
- Discord webhook smoke test:
  - `python scripts/test_discord_webhook.py`

## Maintenance Commands

Primary admin command:
- `python scripts/update_software.py`

Break-glass commands:
- `python scripts/maint_preflight.py`
- `python scripts/maint_backup.py`
- `python scripts/maint_upgrade.py`
- `python scripts/maint_db_status.py`

## Telemetry Notes

- Telemetry is opt-in via first-run admin consent.
- Payload contains only install UUID, version, monitored-character count, and timestamp.
- Collector target URL and 24h emit interval are code-level constants in `app/config.py`.
- Collector ingest is enabled only on nodes where `TELEMETRY_PRIMARY_NODE=true` is manually set in `.env`.

## Deployment

See `DEPLOYMENT.md` for full VPS deployment instructions (pyenv, PostgreSQL, systemd, nginx, TLS).
