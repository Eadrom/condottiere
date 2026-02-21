"""FastAPI entrypoint."""

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes_auth import router as auth_router
from app.api.routes_settings import router as settings_router
from app.api.routes_status import router as status_router
from app.api.routes_telemetry import router as telemetry_router
from app.config import get_settings
from app.db.session import init_database
from app.telemetry.events import ensure_local_install_id
from app.web.routes import router as web_router


app = FastAPI(title="Condottiere")
settings = get_settings()

# Session cookie storage for OAuth state and logged-in character identity.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=settings.env.lower() == "prod",
)


@app.on_event("startup")
def on_startup() -> None:
    init_database()
    ensure_local_install_id()


@app.on_event("shutdown")
def on_shutdown() -> None:
    """Pseudocode shutdown:
    - Close pooled clients and DB sessions cleanly
    """


app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(settings_router, prefix="/settings", tags=["settings"])
app.include_router(status_router, prefix="/status", tags=["status"])
if settings.telemetry_primary_node:
    app.include_router(telemetry_router, prefix="/telemetry", tags=["telemetry"])
app.include_router(web_router, tags=["web"])
