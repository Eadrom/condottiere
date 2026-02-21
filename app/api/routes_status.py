"""Operational status routes for machine monitoring."""

from datetime import UTC, datetime

from fastapi import APIRouter, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import engine

router = APIRouter()


@router.get("/health")
def health(response: Response):
    """Liveness/readiness endpoint suitable for uptime probes."""
    now = datetime.now(UTC).isoformat()
    checks: dict[str, dict[str, str]] = {}

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = {"status": "ok"}
        return {
            "status": "ok",
            "timestamp": now,
            "checks": checks,
        }
    except SQLAlchemyError as exc:
        response.status_code = 503
        checks["database"] = {"status": "error", "detail": str(exc)[:300]}
        return {
            "status": "error",
            "timestamp": now,
            "checks": checks,
        }
