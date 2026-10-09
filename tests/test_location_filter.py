"""Whitelist / blacklist / jump-range filter tests."""

import json

import pytest

from app.notifications.location_filter import (
    NO_FILTER,
    StoredFilterError,
    build_filter,
    evaluate,
    load_filter,
)
from app.universe.model import Universe
from tests.universe_fixture import make_universe


@pytest.fixture
def universe(tmp_path, monkeypatch) -> Universe:
    return make_universe(tmp_path, monkeypatch)


def _filter(universe, list_mode="off", places="", origin="", jumps=""):
    return build_filter(
        universe,
        list_mode=list_mode,
        places_text=places,
        range_origin_text=origin,
        range_jumps_text=jumps,
    )


@pytest.mark.parametrize(
    "places, system_id, whitelisted",
    [
        ("Alpha", 103, True),  # region match
        ("Alpha", 201, False),
        ("Alpha One", 102, True),  # constellation match
        ("Alpha One", 103, False),
        ("B2", 202, True),  # system match
        ("B2", 201, False),
        ("Alpha Two, B1", 201, True),
    ],
)
def test_whitelist_and_blacklist_at_every_granularity(universe, places, system_id, whitelisted):
    white = _filter(universe, "whitelist", places)
    black = _filter(universe, "blacklist", places)
    assert evaluate(white, system_id, universe).send is whitelisted
    assert evaluate(black, system_id, universe).send is (not whitelisted)


def test_range_counts_gate_jumps_without_zarzakh_shortcut(universe):
    within_3 = _filter(universe, origin="A1", jumps="3")
    assert evaluate(within_3, 201, universe).send is True
    decision = evaluate(within_3, 202, universe)
    assert decision.send is False
    assert "more than 3 jumps from A1" in decision.reason


def test_list_and_range_combine_as_and(universe):
    combined = _filter(universe, "blacklist", "Alpha Two", origin="A1", jumps="3")
    assert evaluate(combined, 102, universe).send is True
    assert evaluate(combined, 103, universe).send is False  # in range, but blacklisted
    assert evaluate(combined, 202, universe).send is False  # not blacklisted, out of range


def test_both_off_sends_everything(universe):
    f = _filter(universe)
    assert f == NO_FILTER and f.to_json() == ""
    assert evaluate(f, 202, universe).send is True


def test_unknown_location_fails_open(universe):
    f = _filter(universe, "whitelist", "Alpha")
    assert evaluate(f, None, universe).send is True
    assert evaluate(f, 99999999, universe).send is True


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (dict(list_mode="whitelist", places="Alpha, Nowhere"), "Unknown region, constellation or system: Nowhere"),
        (dict(list_mode="blacklist", places="Gamma"), "Gamma is ambiguous"),
        (dict(list_mode="whitelist", places=" , "), "Add at least one"),
        (dict(origin="Alpha", jumps="3"), "staging point must be a solar system"),
        (dict(origin="A1"), "needs both"),
        (dict(origin="A1", jumps="ten"), "whole number"),
        (dict(origin="A1", jumps="101"), "between 0 and 100"),
        (dict(list_mode="allowlist", places="A1"), "Unknown list mode"),
    ],
)
def test_bad_input_is_rejected_with_the_reason(universe, kwargs, message):
    with pytest.raises(ValueError, match=message):
        _filter(universe, **kwargs)


def test_round_trip_through_storage(universe):
    f = _filter(universe, "whitelist", "alpha one, b2, B2", origin="a1", jumps="5")
    restored = load_filter(f.to_json())
    assert restored == f
    assert [p.name for p in restored.places] == ["Alpha One", "B2"]  # canonical names, deduped
    assert restored.range_origin.name == "A1"


def test_unreadable_stored_filter_raises():
    assert load_filter("") == NO_FILTER
    with pytest.raises(StoredFilterError):
        load_filter("{not json")
    with pytest.raises(StoredFilterError):
        load_filter(json.dumps({"list_mode": "denylist"}))


def test_describe_reads_as_a_sentence(universe):
    from app.notifications.location_filter import describe

    assert describe(_filter(universe)) == "No filter: every alert is sent."
    assert describe(_filter(universe, "blacklist", "Alpha, B2")) == "Blacklist: never alert in Alpha, B2."
    assert describe(_filter(universe, origin="A1", jumps="3")) == "Only within 3 jumps of A1."
    assert (
        describe(_filter(universe, "whitelist", "Alpha", origin="A1", jumps="3"))
        == "Whitelist: only alert in Alpha; and only within 3 jumps of A1."
    )
