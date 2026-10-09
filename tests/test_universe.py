"""Pinned SDE cache and universe model tests."""

import httpx
import pytest

from app.universe import sde
from app.universe.model import Universe, load_universe
from tests.universe_fixture import BUILD, ZARZAKH, ZIP_BYTES, ensure as _ensure, make_universe, mock_client as _mock_client

def test_downloads_pinned_build_once_and_keeps_no_zip(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sde, "_make_client", _mock_client(calls))
    path = _ensure(tmp_path)
    assert calls == [sde.SDE_URL_TEMPLATE.format(build=BUILD)]
    assert "latest" not in calls[0]
    assert path == sde.cache_path(tmp_path, BUILD)
    assert not list(tmp_path.glob("*.zip")) and not list(tmp_path.glob(".*.zip"))

    _ensure(tmp_path)
    assert len(calls) == 1, "second call must use the cache, not download again"


def test_checksum_mismatch_is_refused_and_nothing_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(sde, "_make_client", _mock_client([], body=ZIP_BYTES + b"tampered"))
    with pytest.raises(sde.UniverseUnavailable, match="checksum mismatch"):
        _ensure(tmp_path)
    assert not sde.cache_path(tmp_path, BUILD).exists()
    assert not list(tmp_path.glob(".*.zip"))


def test_http_failure_raises_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(sde, "_make_client", _mock_client([], status=404))
    with pytest.raises(sde.UniverseUnavailable, match="download failed"):
        _ensure(tmp_path)


def test_cache_deleted_externally_is_rebuilt(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sde, "_make_client", _mock_client(calls))
    path = _ensure(tmp_path)
    path.unlink()
    assert load_universe(tmp_path, BUILD) is None
    _ensure(tmp_path)
    assert len(calls) == 2
    assert load_universe(tmp_path, BUILD) is not None


@pytest.fixture
def universe(tmp_path, monkeypatch) -> Universe:
    return make_universe(tmp_path, monkeypatch)


def test_find_is_case_insensitive_across_kinds(universe):
    assert [(p.kind, p.id) for p in universe.find("alpha")] == [("region", 1)]
    assert [(p.kind, p.id) for p in universe.find("  alpha one ")] == [("constellation", 11)]
    assert [(p.kind, p.id) for p in universe.find("b2")] == [("system", 202)]
    assert {p.kind for p in universe.find("Gamma")} == {"region", "system"}
    assert universe.find("Nowhere") == []


def test_zarzakh_is_a_destination_not_a_shortcut(universe):
    assert universe.jumps_between(101, 202, max_jumps=10) == 4
    assert universe.jumps_between(101, ZARZAKH, max_jumps=10) == 1
    assert universe.jumps_between(ZARZAKH, 202, max_jumps=10) == 1


def test_jump_limit_and_unreachable(universe):
    assert universe.jumps_between(101, 101, max_jumps=0) == 0
    assert universe.jumps_between(101, 201, max_jumps=3) == 3
    assert universe.jumps_between(101, 201, max_jumps=2) is None
    assert universe.jumps_between(101, 301, max_jumps=100) is None
