"""FastAPI entrypoint."""

from typing import Awaitable, Callable

from fastapi import FastAPI
from fastapi import Request
from fastapi import Response
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes_auth import router as auth_router
from app.api.routes_settings import router as settings_router
from app.api.routes_status import router as status_router
from app.api.routes_telemetry import router as telemetry_router
from app.config import get_settings
from app.db.session import init_database
from app.telemetry.events import ensure_local_install_id
from app.web.routes import router as web_router


settings = get_settings()
app = FastAPI(
    title="Condottiere",
    openapi_url=None if settings.env.lower() == "prod" else "/openapi.json",
    docs_url=None if settings.env.lower() == "prod" else "/docs",
    redoc_url=None if settings.env.lower() == "prod" else "/redoc",
)

# Session cookie storage for OAuth state and logged-in character identity.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=settings.env.lower() == "prod",
)


_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' https://images.evetech.net data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)
_PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), payment=(), usb=()"
)


@app.middleware("http")
async def security_headers_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)

    # Safe browser defaults for clickjacking/MIME sniffing/privacy controls.
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", _PERMISSIONS_POLICY)
    response.headers.setdefault("Content-Security-Policy", _CONTENT_SECURITY_POLICY)

    # HSTS should only be emitted for HTTPS responses.
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


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
