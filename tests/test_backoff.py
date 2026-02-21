"""Backoff scheduling tests."""

from app.services.backoff import compute_backoff_seconds


def test_backoff_progression_and_cap():
    assert compute_backoff_seconds(1) == 30
    assert compute_backoff_seconds(2) == 60
    assert compute_backoff_seconds(3) == 120
    assert compute_backoff_seconds(4) == 300
    assert compute_backoff_seconds(5) == 600
    assert compute_backoff_seconds(6) == 600
    assert compute_backoff_seconds(12) == 600
