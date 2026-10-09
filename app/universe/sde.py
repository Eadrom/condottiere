"""Pinned SDE download and compact universe cache.

The SDE build is pinned here and only changes with a Condottiere release, so a new
CCP export can never change filtering behaviour on a running server. On first use
the pinned zip is downloaded from CCP, checked against its sha256, and reduced to
a small JSON cache of the map data. The zip itself is not kept.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile

import httpx

PINNED_SDE_BUILD = 3586130
PINNED_SDE_SHA256 = "b6efc200ec30911e5d22decc8ee3c34cafeac1ecc49faa76491fd75fa6c36045"
SDE_URL_TEMPLATE = (
    "https://developers.eveonline.com/static-data/tranquility/"
    "eve-online-static-data-{build}-jsonl.zip"
)
CACHE_FORMAT_VERSION = 1

_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=20.0, read=60.0, write=60.0, pool=20.0)
_CHUNK_BYTES = 1 << 20


class UniverseUnavailable(RuntimeError):
    """The pinned universe data is not on disk and could not be built right now."""


def cache_path(data_dir: Path, build: int = PINNED_SDE_BUILD) -> Path:
    return Path(data_dir) / f"universe-{build}.json"


def _english_name(record: dict) -> str:
    name = record.get("name")
    if isinstance(name, dict):
        return str(name.get("en", "")).strip()
    return str(name or "").strip()


def _read_jsonl(archive: zipfile.ZipFile, member: str):
    with archive.open(member) as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def build_cache_from_zip(zip_path: Path, *, build: int) -> dict:
    """Reduce an SDE JSONL zip to the map data Condottiere needs."""
    with zipfile.ZipFile(zip_path) as archive:
        regions = {
            int(row["_key"]): _english_name(row)
            for row in _read_jsonl(archive, "mapRegions.jsonl")
        }
        constellations = {
            int(row["_key"]): [_english_name(row), int(row["regionID"])]
            for row in _read_jsonl(archive, "mapConstellations.jsonl")
        }
        systems = {
            int(row["_key"]): [
                _english_name(row),
                int(row["constellationID"]),
                int(row["regionID"]),
            ]
            for row in _read_jsonl(archive, "mapSolarSystems.jsonl")
        }
        gates = sorted(
            {
                (int(row["solarSystemID"]), int(row["destination"]["solarSystemID"]))
                for row in _read_jsonl(archive, "mapStargates.jsonl")
            }
        )

    if not regions or not constellations or not systems or not gates:
        raise UniverseUnavailable("SDE zip is missing map data")

    return {
        "format": CACHE_FORMAT_VERSION,
        "sde_build": build,
        "regions": {str(k): v for k, v in regions.items()},
        "constellations": {str(k): v for k, v in constellations.items()},
        "systems": {str(k): v for k, v in systems.items()},
        "gates": [list(pair) for pair in gates],
    }


def _write_json_atomic(path: Path, payload: dict) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _make_client() -> httpx.Client:
    return httpx.Client(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True)


def _download_verified(url: str, dest: Path, expected_sha256: str, *, user_agent: str) -> None:
    digest = hashlib.sha256()
    with _make_client() as client:
        with client.stream("GET", url, headers={"User-Agent": user_agent}) as response:
            response.raise_for_status()
            with open(dest, "wb") as handle:
                for chunk in response.iter_bytes(_CHUNK_BYTES):
                    digest.update(chunk)
                    handle.write(chunk)
    actual = digest.hexdigest()
    if actual != expected_sha256:
        raise UniverseUnavailable(
            f"SDE build checksum mismatch (expected {expected_sha256[:12]}..., got {actual[:12]}...)"
        )


def ensure_universe_cache(
    data_dir: Path,
    *,
    user_agent: str,
    build: int = PINNED_SDE_BUILD,
    expected_sha256: str = PINNED_SDE_SHA256,
    url_template: str = SDE_URL_TEMPLATE,
) -> Path:
    """Return the cache path for the pinned build, downloading and building it if missing.

    Raises UniverseUnavailable if the cache is absent and could not be built, including
    when another process holds the build lock (it is already downloading).
    """
    data_dir = Path(data_dir)
    target = cache_path(data_dir, build)
    if target.is_file():
        return target

    data_dir.mkdir(parents=True, exist_ok=True)
    lock_file = open(data_dir / ".build.lock", "w")
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UniverseUnavailable("universe data is being built by another process") from exc

        if target.is_file():
            return target

        fd, zip_name = tempfile.mkstemp(dir=data_dir, prefix=f".sde-{build}.", suffix=".zip")
        os.close(fd)
        zip_path = Path(zip_name)
        try:
            try:
                _download_verified(
                    url_template.format(build=build),
                    zip_path,
                    expected_sha256,
                    user_agent=user_agent,
                )
            except httpx.HTTPError as exc:
                raise UniverseUnavailable(f"SDE download failed: {exc}") from exc
            try:
                payload = build_cache_from_zip(zip_path, build=build)
            except (zipfile.BadZipFile, KeyError, ValueError) as exc:
                raise UniverseUnavailable(f"SDE zip could not be read: {exc}") from exc
            _write_json_atomic(target, payload)
        finally:
            zip_path.unlink(missing_ok=True)
        return target
    finally:
        lock_file.close()
