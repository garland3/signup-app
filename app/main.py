import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from app.core.config import get_settings
from app.core.middleware import AuthMiddleware
from app.core.sessions import InMemorySessionMiddleware
from app.routes.auth import router as auth_router
from app.routes.health import router as health_router
from app.routes.users import router as users_router
from app.routes.keys import router as keys_router

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"

# Routes that must never be cached by browsers or shared proxies.
NO_STORE_PATHS = (
    "/api/me",
    "/api/keys",
    "/api/config",
    "/api/auth/",
)


def _enforce_startup_safety(settings) -> None:
    """Refuse to boot in obviously-unsafe configurations.

    This is the last line of defence against a production deployment
    accidentally inheriting development defaults.
    """
    problems: list[str] = []

    if settings.DEBUG_MODE and settings.AUTH_MODE == "oauth":
        problems.append(
            "DEBUG_MODE=true is not permitted with AUTH_MODE=oauth "
            "(would enable ALLOW_TEST_USER bypass on a cookie-authenticated app)"
        )

    if settings.ALLOW_TEST_USER and not settings.DEBUG_MODE:
        problems.append(
            "ALLOW_TEST_USER=true requires DEBUG_MODE=true; this flag is "
            "development-only and must never be set in production"
        )

    if settings.AUTH_MODE == "proxy":
        if settings.FEATURE_PROXY_SECRET_ENABLED and not settings.PROXY_SECRET:
            problems.append(
                "FEATURE_PROXY_SECRET_ENABLED=true requires a non-empty "
                "PROXY_SECRET"
            )
        if (
            not settings.FEATURE_PROXY_SECRET_ENABLED
            and not settings.DEBUG_MODE
            and not settings.ALLOW_INSECURE_STARTUP
        ):
            problems.append(
                "Proxy-mode without a shared secret is unsafe. Set "
                "FEATURE_PROXY_SECRET_ENABLED=true and PROXY_SECRET=..., "
                "or (for development only) set DEBUG_MODE=true / "
                "ALLOW_INSECURE_STARTUP=true"
            )

    if settings.AUTH_MODE == "oauth" and not settings.SESSION_SECRET:
        problems.append("SESSION_SECRET must be set when AUTH_MODE=oauth")

    if problems:
        raise RuntimeError(
            "Refusing to start due to insecure configuration:\n  - "
            + "\n  - ".join(problems)
        )

    # Warn (but don't block) when cookie Secure is being silently downgraded
    # because DEBUG_MODE is on. Surfaces the footgun noted in the review.
    if (
        settings.AUTH_MODE == "oauth"
        and settings.SESSION_COOKIE_SECURE
        and settings.DEBUG_MODE
    ):
        logger.warning(
            "SESSION_COOKIE_SECURE=true is being downgraded to false "
            "because DEBUG_MODE=true. Cookies will NOT require HTTPS. "
            "Do not run this configuration in production."
        )


def create_app():
    settings = get_settings()
    _enforce_startup_safety(settings)

    root_path = settings.normalized_root_path
    # Intentionally do NOT pass root_path= to FastAPI: we prefix routes
    # explicitly instead, which keeps path matching and URL generation
    # consistent regardless of whether a reverse proxy strips the prefix.
    app = FastAPI(title="Signup App")
    app.state.settings = settings

    # AuthMiddleware is added first so it runs *after* SessionMiddleware on the
    # request (Starlette wraps middleware in reverse order). This guarantees
    # request.session is available when AuthMiddleware checks for an OAuth user.
    app.add_middleware(AuthMiddleware, settings=settings)
    if settings.AUTH_MODE == "oauth":
        app.add_middleware(
            InMemorySessionMiddleware,
            cookie_name=settings.SESSION_COOKIE_NAME,
            max_age=settings.SESSION_MAX_AGE,
            idle_timeout=settings.SESSION_IDLE_TIMEOUT,
            same_site="lax",
            https_only=settings.SESSION_COOKIE_SECURE and not settings.DEBUG_MODE,
        )

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        # Strict CSP: no inline scripts, no remote resources. The frontend
        # loads APP_ROOT_PATH from a <meta> tag so it needs no inline JS.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "object-src 'none'"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if not settings.DEBUG_MODE:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        # Never cache sensitive API responses.
        path = request.url.path
        stripped = (
            path[len(root_path):]
            if root_path and path.startswith(root_path)
            else path
        )
        if any(stripped.startswith(p) for p in NO_STORE_PATHS):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    app.include_router(health_router, prefix=root_path)
    app.include_router(auth_router, prefix=root_path)
    app.include_router(users_router, prefix=root_path)
    app.include_router(keys_router, prefix=root_path)

    if STATIC_DIR.exists():
        app.mount(
            f"{root_path}/static", StaticFiles(directory=str(STATIC_DIR))
        )

        # Render index.html with the configured root path substituted so
        # asset URLs and the frontend API base path are prefix-aware.
        index_html = (STATIC_DIR / "index.html").read_text().replace(
            "{{ROOT_PATH}}", root_path
        )

        @app.get(f"{root_path}/")
        async def serve_frontend():
            return HTMLResponse(index_html)

        if root_path:
            # Direct visits to the container root redirect to the prefix
            # so users who browse to the bare host still land in the app.
            @app.get("/")
            async def root_redirect():
                return RedirectResponse(f"{root_path}/", status_code=307)

    return app


app = create_app()
