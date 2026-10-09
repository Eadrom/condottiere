"""A tiny synthetic SDE for universe and filter tests."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

ZARZAKH = 30100000

REGIONS = {1: "Alpha", 2: "Beta", 9: "Yasna Zakh", 3: "Gamma"}
CONSTELLATIONS = {11: ("Alpha One", 1), 12: ("Alpha Two", 1), 21: ("Beta One", 2), 99: ("Zakh", 9), 31: ("Isles", 3)}
SYSTEMS = {
    101: ("A1", 11, 1),
    102: ("A2", 11, 1),
    103: ("A3", 12, 1),
    201: ("B1", 21, 2),
    202: ("B2", 21, 2),
    ZARZAKH: ("Zarzakh", 99, 9),
    301: ("Isle", 31, 3),
    302: ("Gamma", 31, 3),  # same name as the Gamma region: ambiguous
}
# A1-A2-A3-B1-B2 chain; Zarzakh links A1 and B2 but must not be a shortcut; Isle has no gates.
LINKS = [(101, 102), (102, 103), (103, 201), (201, 202), (101, ZARZAKH), (ZARZAKH, 202)]


def _names(en: str) -> dict:
    return {"en": en, "de": en}


def build_zip_bytes() -> bytes:
    gate_id = 50000000
    gates = []
    for a, b in LINKS:
        gates.append({"_key": gate_id, "solarSystemID": a, "destination": {"solarSystemID": b, "stargateID": gate_id + 1}, "typeID": 1})
        gates.append({"_key": gate_id + 1, "solarSystemID": b, "destination": {"solarSystemID": a, "stargateID": gate_id}, "typeID": 1})
        gate_id += 2
    files = {
        "mapRegions.jsonl": [{"_key": k, "name": _names(v)} for k, v in REGIONS.items()],
        "mapConstellations.jsonl": [{"_key": k, "name": _names(n), "regionID": r} for k, (n, r) in CONSTELLATIONS.items()],
        "mapSolarSystems.jsonl": [{"_key": k, "name": _names(n), "constellationID": c, "regionID": r} for k, (n, c, r) in SYSTEMS.items()],
        "mapStargates.jsonl": gates,
        "_sde.jsonl": [{"_key": "sde", "buildNumber": 1}],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, rows in files.items():
            archive.writestr(name, "\n".join(json.dumps(r) for r in rows) + "\n")
    return buffer.getvalue()


ZIP_BYTES = build_zip_bytes()
ZIP_SHA256 = hashlib.sha256(ZIP_BYTES).hexdigest()

BUILD = 1


def mock_client(calls: list, body: bytes = ZIP_BYTES, status: int = 200):
    """Factory for app.universe.sde._make_client that serves the fixture zip."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, content=body)

    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def ensure(tmp_path, **overrides):
    from app.universe import sde

    kwargs = dict(user_agent="test", build=BUILD, expected_sha256=ZIP_SHA256)
    kwargs.update(overrides)
    return sde.ensure_universe_cache(tmp_path, **kwargs)


def make_universe(tmp_path, monkeypatch):
    from app.universe import sde
    from app.universe.model import Universe

    monkeypatch.setattr(sde, "_make_client", mock_client([]))
    return Universe(json.loads(ensure(tmp_path).read_text()))
