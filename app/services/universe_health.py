"""Keep the pinned universe data available to the sender, healing it if it goes missing.

If the cache is missing when an alert that needs it is about to send, it is rebuilt
from the pinned SDE build (at most once per sender run; held alerts come due again
every few minutes). While that keeps failing, those alerts are held, and the admin
gets at most one EVE mail per day. The next successful build clears that daily lock.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from app.db.models import AppState
from app.universe.model import Universe, load_universe
from app.universe.sde import (
    PINNED_SDE_BUILD,
    UniverseBusy,
    UniverseUnavailable,
    cache_path,
    ensure_universe_cache,
)

ADMIN_MAIL_INTERVAL = timedelta(hours=24)

_ADMIN_MAILED_KEY = "universe_admin_mailed_at"

NotifyAdmin = Callable[[str, str], tuple[bool, str | None]]


def _get_time(db, key: str) -> datetime | None:
    row = db.get(AppState, key)
    if row is None:
        return None
    try:
        return datetime.fromisoformat(row.value)
    except ValueError:
        return None


def _set_time(db, key: str, value: datetime) -> None:
    row = db.get(AppState, key)
    if row is None:
        db.add(AppState(key=key, value=value.isoformat()))
    else:
        row.value = value.isoformat()


def _clear(db, key: str) -> None:
    row = db.get(AppState, key)
    if row is not None:
        db.delete(row)


class UniverseGate:
    """Lazily provides the universe for one sender cycle; builds it at most once per cycle."""

    def __init__(self, db, *, data_dir: str, user_agent: str, now: datetime, notify_admin: NotifyAdmin):
        self._db = db
        self._data_dir = Path(data_dir)
        self._user_agent = user_agent
        self._now = now
        self._notify_admin = notify_admin
        self._resolved = False
        self._universe: Universe | None = None
        self.unavailable_reason: str | None = None

    def get(self) -> Universe | None:
        if not self._resolved:
            self._resolved = True
            self._universe = self._resolve()
        return self._universe

    def _resolve(self) -> Universe | None:
        # Never let a bad cache or a failed build escape: an exception here would stop
        # the whole sender run, including unfiltered alerts and timer warnings.
        try:
            universe = load_universe(self._data_dir)
        except Exception as exc:  # noqa: BLE001 - corrupt or unreadable cache
            print("universe", f"cache-unreadable={exc!r}", "action=rebuild")
            cache_path(self._data_dir).unlink(missing_ok=True)
            universe = None
        if universe is not None:
            _clear(self._db, _ADMIN_MAILED_KEY)
            return universe

        try:
            ensure_universe_cache(self._data_dir, user_agent=self._user_agent)
            universe = load_universe(self._data_dir)
        except UniverseBusy as exc:
            # The web service is downloading it right now; hold quietly, no admin mail.
            self.unavailable_reason = f"universe data unavailable: {exc}"
            return None
        except Exception as exc:  # noqa: BLE001 - UniverseUnavailable, OSError, bad data
            self.unavailable_reason = f"universe data unavailable: {exc}"
            self._maybe_mail_admin(str(exc))
            return None

        if universe is None:
            self.unavailable_reason = "universe data unavailable after rebuild"
            self._maybe_mail_admin(self.unavailable_reason)
            return None
        _clear(self._db, _ADMIN_MAILED_KEY)
        print("universe", f"rebuilt sde_build={universe.sde_build}")
        return universe

    def _maybe_mail_admin(self, problem: str) -> None:
        last_mailed = _get_time(self._db, _ADMIN_MAILED_KEY)
        if last_mailed is not None and self._now - last_mailed < ADMIN_MAIL_INTERVAL:
            return
        subject = "Condottiere: universe data unavailable"
        body = (
            "Condottiere could not load or rebuild its universe data "
            f"(SDE build {PINNED_SDE_BUILD}).\n\n"
            f"Problem: {problem}\n\n"
            "Alerts that use a location filter are being held until this is fixed. "
            "Alerts without a filter are still sent. Held alerts expire after 24 hours.\n\n"
            f"Cache directory: {self._data_dir}"
        )
        # Claim the daily slot before sending, so a failed save can never cause repeat mails.
        _set_time(self._db, _ADMIN_MAILED_KEY, self._now)
        self._db.commit()
        ok, error = self._notify_admin(subject, body)
        if not ok:
            _clear(self._db, _ADMIN_MAILED_KEY)
        self._db.commit()
        print("universe", f"admin-mail={'sent' if ok else 'failed'}", f"error={error or '-'}")
