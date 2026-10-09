"""Location-based alert filtering: whitelist/blacklist of places, plus a jump range."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re

from app.universe.model import KIND_CONSTELLATION, KIND_REGION, KIND_SYSTEM, Universe

LIST_OFF = "off"
LIST_WHITELIST = "whitelist"
LIST_BLACKLIST = "blacklist"
LIST_MODES = (LIST_OFF, LIST_WHITELIST, LIST_BLACKLIST)

MAX_ENTRIES = 100
MAX_RANGE_JUMPS = 100
_KINDS = (KIND_REGION, KIND_CONSTELLATION, KIND_SYSTEM)


@dataclass(frozen=True)
class PlaceRef:
    kind: str
    id: int
    name: str


@dataclass(frozen=True)
class AlertFilter:
    list_mode: str = LIST_OFF
    places: tuple[PlaceRef, ...] = field(default_factory=tuple)
    range_origin: PlaceRef | None = None
    range_max_jumps: int | None = None

    @property
    def list_active(self) -> bool:
        return self.list_mode in (LIST_WHITELIST, LIST_BLACKLIST) and bool(self.places)

    @property
    def range_active(self) -> bool:
        return self.range_origin is not None and self.range_max_jumps is not None

    @property
    def active(self) -> bool:
        return self.list_active or self.range_active

    def to_json(self) -> str:
        if not self.active:
            return ""
        payload: dict = {"list_mode": self.list_mode if self.list_active else LIST_OFF}
        if self.list_active:
            payload["places"] = [
                {"kind": p.kind, "id": p.id, "name": p.name} for p in self.places
            ]
        if self.range_active:
            payload["range"] = {
                "origin_id": self.range_origin.id,
                "origin_name": self.range_origin.name,
                "max_jumps": self.range_max_jumps,
            }
        return json.dumps(payload, separators=(",", ":"))


NO_FILTER = AlertFilter()


class StoredFilterError(ValueError):
    """A stored filter could not be read. Callers must not treat this as 'no filter'."""


def load_filter(raw: str | None) -> AlertFilter:
    """Parse a stored filter. Empty means no filter; anything unreadable raises."""
    if not (raw or "").strip():
        return NO_FILTER
    try:
        payload = json.loads(raw)
        list_mode = payload.get("list_mode", LIST_OFF)
        if list_mode not in LIST_MODES:
            raise ValueError(f"unknown list mode {list_mode!r}")
        places = tuple(
            PlaceRef(str(p["kind"]), int(p["id"]), str(p["name"]))
            for p in payload.get("places", [])
        )
        if any(p.kind not in _KINDS for p in places):
            raise ValueError("unknown place kind")
        range_origin = None
        range_max_jumps = None
        if payload.get("range"):
            rng = payload["range"]
            range_origin = PlaceRef(KIND_SYSTEM, int(rng["origin_id"]), str(rng["origin_name"]))
            range_max_jumps = int(rng["max_jumps"])
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise StoredFilterError(f"stored alert filter is unreadable: {exc}") from exc
    return AlertFilter(list_mode, places, range_origin, range_max_jumps)


def _resolve_place(universe: Universe, name: str) -> PlaceRef:
    matches = universe.find(name)
    if not matches:
        raise ValueError(f"Unknown region, constellation or system: {name}")
    if len(matches) > 1:
        kinds = " and ".join(sorted({m.kind for m in matches}))
        raise ValueError(f"{name} is ambiguous (it names a {kinds})")
    match = matches[0]
    return PlaceRef(match.kind, match.id, match.name)


def build_filter(
    universe: Universe,
    *,
    list_mode: str,
    places_text: str,
    range_origin_text: str,
    range_jumps_text: str,
) -> AlertFilter:
    """Validate form input into a filter. Raises ValueError with a user-facing message."""
    list_mode = (list_mode or LIST_OFF).strip().lower()
    if list_mode not in LIST_MODES:
        raise ValueError("Unknown list mode.")

    places: list[PlaceRef] = []
    if list_mode != LIST_OFF:
        names = [n.strip() for n in re.split(r"[,\n]", places_text or "") if n.strip()]
        if not names:
            raise ValueError(f"Add at least one region, constellation or system to the {list_mode}.")
        if len(names) > MAX_ENTRIES:
            raise ValueError(f"The {list_mode} can hold at most {MAX_ENTRIES} entries.")
        seen: set[tuple[str, int]] = set()
        for name in names:
            place = _resolve_place(universe, name)
            if (place.kind, place.id) not in seen:
                seen.add((place.kind, place.id))
                places.append(place)

    range_origin = None
    range_max_jumps = None
    origin_text = (range_origin_text or "").strip()
    jumps_text = (range_jumps_text or "").strip()
    if origin_text or jumps_text:
        if not origin_text or not jumps_text:
            raise ValueError("A jump range needs both a staging system and a number of jumps.")
        origin = _resolve_place(universe, origin_text)
        if origin.kind != KIND_SYSTEM:
            raise ValueError(f"{origin.name} is a {origin.kind}; the staging point must be a solar system.")
        try:
            range_max_jumps = int(jumps_text)
        except ValueError as exc:
            raise ValueError("Jump range must be a whole number.") from exc
        if not 0 <= range_max_jumps <= MAX_RANGE_JUMPS:
            raise ValueError(f"Jump range must be between 0 and {MAX_RANGE_JUMPS}.")
        range_origin = origin

    return AlertFilter(list_mode, tuple(places), range_origin, range_max_jumps)


@dataclass(frozen=True)
class FilterDecision:
    send: bool
    reason: str


def evaluate(alert_filter: AlertFilter, system_id: int | None, universe: Universe) -> FilterDecision:
    """Decide whether an alert in system_id passes the filter. Unknown location fails open."""
    if not alert_filter.active:
        return FilterDecision(True, "no filter")
    if system_id is None or system_id not in universe.systems:
        return FilterDecision(True, f"location unknown (system {system_id}); sent unfiltered")

    system_name = universe.system_name(system_id) or str(system_id)

    if alert_filter.list_active:
        listed = next(
            (p for p in alert_filter.places if universe.contains(p.kind, p.id, system_id)),
            None,
        )
        if alert_filter.list_mode == LIST_WHITELIST and listed is None:
            return FilterDecision(False, f"{system_name} is not in the whitelist")
        if alert_filter.list_mode == LIST_BLACKLIST and listed is not None:
            return FilterDecision(False, f"{system_name} is blacklisted ({listed.kind} {listed.name})")

    if alert_filter.range_active:
        jumps = universe.jumps_between(
            alert_filter.range_origin.id, system_id, max_jumps=alert_filter.range_max_jumps
        )
        if jumps is None:
            return FilterDecision(
                False,
                f"{system_name} is more than {alert_filter.range_max_jumps} jumps "
                f"from {alert_filter.range_origin.name}",
            )

    return FilterDecision(True, "passed filter")


def describe(alert_filter: AlertFilter) -> str:
    """One-line human summary for settings pages."""
    if not alert_filter.active:
        return "No filter: every alert is sent."
    parts = []
    if alert_filter.list_active:
        names = ", ".join(p.name for p in alert_filter.places)
        label = "only alert in" if alert_filter.list_mode == LIST_WHITELIST else "never alert in"
        parts.append(f"{alert_filter.list_mode.capitalize()}: {label} {names}")
    if alert_filter.range_active:
        parts.append(
            f"only within {alert_filter.range_max_jumps} jumps of {alert_filter.range_origin.name}"
        )
    text = "; and ".join(parts)
    return text[0].upper() + text[1:] + "."


def form_values(alert_filter: AlertFilter) -> dict[str, str]:
    return {
        "list_mode": alert_filter.list_mode if alert_filter.list_active else LIST_OFF,
        "places": ", ".join(p.name for p in alert_filter.places) if alert_filter.list_active else "",
        "range_origin": alert_filter.range_origin.name if alert_filter.range_active else "",
        "range_jumps": str(alert_filter.range_max_jumps) if alert_filter.range_active else "",
    }
