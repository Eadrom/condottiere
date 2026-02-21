"""Retry backoff helpers."""

BACKOFF_SCHEDULE_SECONDS: tuple[int, ...] = (30, 60, 120, 300, 600)


def compute_backoff_seconds(attempt_number: int) -> int:
    """Apply capped backoff schedule."""
    idx = min(max(attempt_number - 1, 0), len(BACKOFF_SCHEDULE_SECONDS) - 1)
    return BACKOFF_SCHEDULE_SECONDS[idx]
