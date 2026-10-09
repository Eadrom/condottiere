"""In-memory view of the cached universe: names, hierarchy, and stargate jumps."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path

from app.universe.sde import CACHE_FORMAT_VERSION, PINNED_SDE_BUILD, cache_path

# Entering Zarzakh locks you to the gate you came in by, so it can be a destination
# but never a shortcut across the cluster.
DESTINATION_ONLY_SYSTEM_IDS = frozenset({30100000})  # Zarzakh

KIND_REGION = "region"
KIND_CONSTELLATION = "constellation"
KIND_SYSTEM = "system"


@dataclass(frozen=True)
class Place:
    kind: str
    id: int
    name: str


class Universe:
    def __init__(self, payload: dict):
        if payload.get("format") != CACHE_FORMAT_VERSION:
            raise ValueError("unsupported universe cache format")
        self.sde_build = int(payload["sde_build"])
        self.regions: dict[int, str] = {int(k): v for k, v in payload["regions"].items()}
        self.constellations: dict[int, tuple[str, int]] = {
            int(k): (v[0], int(v[1])) for k, v in payload["constellations"].items()
        }
        self.systems: dict[int, tuple[str, int, int]] = {
            int(k): (v[0], int(v[1]), int(v[2])) for k, v in payload["systems"].items()
        }
        self._neighbours: dict[int, list[int]] = {}
        for a, b in payload["gates"]:
            self._neighbours.setdefault(int(a), []).append(int(b))

        self._by_name: dict[str, list[Place]] = {}
        for region_id, name in self.regions.items():
            self._index(Place(KIND_REGION, region_id, name))
        for constellation_id, (name, _) in self.constellations.items():
            self._index(Place(KIND_CONSTELLATION, constellation_id, name))
        for system_id, (name, _, _) in self.systems.items():
            self._index(Place(KIND_SYSTEM, system_id, name))

    def _index(self, place: Place) -> None:
        if place.name:
            self._by_name.setdefault(place.name.casefold(), []).append(place)

    def find(self, name: str) -> list[Place]:
        """Exact, case-insensitive name lookup across regions, constellations and systems."""
        return list(self._by_name.get(name.strip().casefold(), []))

    def system_name(self, system_id: int) -> str | None:
        system = self.systems.get(system_id)
        return system[0] if system else None

    def contains(self, place_kind: str, place_id: int, system_id: int) -> bool:
        system = self.systems.get(system_id)
        if system is None:
            return False
        _, constellation_id, region_id = system
        if place_kind == KIND_SYSTEM:
            return place_id == system_id
        if place_kind == KIND_CONSTELLATION:
            return place_id == constellation_id
        if place_kind == KIND_REGION:
            return place_id == region_id
        return False

    def jumps_between(self, origin_id: int, target_id: int, *, max_jumps: int) -> int | None:
        """Stargate jumps from origin to target, or None if more than max_jumps or unreachable."""
        if origin_id == target_id:
            return 0
        seen = {origin_id}
        queue = deque([(origin_id, 0)])
        while queue:
            current, distance = queue.popleft()
            if distance >= max_jumps:
                continue
            if current in DESTINATION_ONLY_SYSTEM_IDS and current != origin_id:
                continue
            for neighbour in self._neighbours.get(current, ()):
                if neighbour in seen:
                    continue
                if neighbour == target_id:
                    return distance + 1
                seen.add(neighbour)
                queue.append((neighbour, distance + 1))
        return None


@lru_cache(maxsize=2)
def _load(path: str, mtime_ns: int) -> Universe:
    with open(path) as handle:
        return Universe(json.load(handle))


def load_universe(data_dir: Path, build: int = PINNED_SDE_BUILD) -> Universe | None:
    """Load the pinned build's cache if present; None if it is not on disk."""
    path = cache_path(data_dir, build)
    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    return _load(str(path), mtime_ns)
