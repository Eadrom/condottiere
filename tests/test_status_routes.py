"""Tests for machine-readable status endpoint handlers."""

from fastapi import Response

from app.api.routes_status import health


def test_health_handler_ok():
    response = Response()
    payload = health(response)
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert "timestamp" in payload
    assert payload["checks"]["database"]["status"] == "ok"
