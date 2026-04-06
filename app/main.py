import hashlib
from pathlib import Path

import itsdangerous
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware as _SessionMiddleware

from starlette.requests import Request

from app.core.config import get_settings
from app.core.middleware import AuthMiddleware
from app.routes.auth import router as auth_router
from app.routes.health import router as health_router
from app.routes.users import router as users_router
from app.routes.keys import router as keys_router

STATIC_DIR = Path(__file__).parent.parent / "static"


class FipsSafeSessionMiddleware(_SessionMiddleware):
    """SessionMiddleware that signs cookies with HMAC-SHA256.

    Starlette's default uses itsdangerous, which defaults to HMAC-SHA1.
    SHA-1 is disabled in FIPS-enabled environments (e.g. RHEL/UBI base
    images with system FIPS mode), so we override the signer to use
    SHA-256.
    """

    def __init__(self, app, secret_key, **kwargs):
        super().__init__(app, secret_key, **kwargs)
        self.signer = itsdangerous.TimestampSigner(
            str(secret_key), digest_method=hashlib.sha256
        )


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Signup App")
    app.state.settings = settings

    # AuthMiddleware is added first so it runs *after* SessionMiddleware on the
    # request (Starlette wraps middleware in reverse order). This guarantees
    # request.session is available when AuthMiddleware checks for an OAuth user.
    app.add_middleware(AuthMiddleware, settings=settings)
    if settings.AUTH_MODE == "oauth":
        if not settings.SESSION_SECRET:
            raise RuntimeError(
                "SESSION_SECRET must be set when AUTH_MODE=oauth"
            )
        app.add_middleware(
            FipsSafeSessionMiddleware,
            secret_key=settings.SESSION_SECRET,
            session_cookie=settings.SESSION_COOKIE_NAME,
            max_age=settings.SESSION_MAX_AGE,
            same_site="lax",
            https_only=settings.SESSION_COOKIE_SECURE and not settings.DEBUG_MODE,
        )

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if not settings.DEBUG_MODE:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(keys_router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)))

        @app.get("/")
        async def serve_frontend():
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
