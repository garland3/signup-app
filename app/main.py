from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import get_settings
from app.core.middleware import AuthMiddleware
from app.routes.auth import router as auth_router
from app.routes.health import router as health_router
from app.routes.users import router as users_router
from app.routes.keys import router as keys_router

STATIC_DIR = Path(__file__).parent.parent / "static"


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
            SessionMiddleware,
            secret_key=settings.SESSION_SECRET,
            session_cookie=settings.SESSION_COOKIE_NAME,
            max_age=settings.SESSION_MAX_AGE,
            same_site="lax",
            https_only=settings.SESSION_COOKIE_SECURE and not settings.DEBUG_MODE,
        )

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
