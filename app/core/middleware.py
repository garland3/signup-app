import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import Settings

PUBLIC_PATHS = {"/api/health"}
PUBLIC_PREFIXES = ("/api/auth/",)


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Allow public paths
        if path in PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)

        if any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        # Skip non-API paths (serve frontend)
        if not path.startswith("/api"):
            return await call_next(request)

        if self.settings.AUTH_MODE == "oauth":
            user_email = self._resolve_oauth_user(request)
        else:
            user_email = self._resolve_proxy_user(request)
            if isinstance(user_email, JSONResponse):
                return user_email

        if not user_email:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        if self.settings.STRIP_USER_DOMAIN and "@" in user_email:
            user_email = user_email.split("@", 1)[0]

        request.state.user_email = user_email
        return await call_next(request)

    def _resolve_proxy_user(self, request: Request):
        # Proxy secret check (production only)
        if (
            self.settings.FEATURE_PROXY_SECRET_ENABLED
            and not self.settings.DEBUG_MODE
        ):
            proxy_secret = request.headers.get(self.settings.PROXY_SECRET_HEADER, "")
            if not hmac.compare_digest(proxy_secret, self.settings.PROXY_SECRET):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        user_email = request.headers.get(self.settings.AUTH_USER_HEADER)

        # Debug mode fallback
        if not user_email and self.settings.DEBUG_MODE:
            user_email = self.settings.TEST_USER

        return user_email

    def _resolve_oauth_user(self, request: Request):
        session = getattr(request, "session", None)
        if session is None:
            return None
        user_email = session.get("user_email")
        if not user_email and self.settings.DEBUG_MODE:
            user_email = self.settings.TEST_USER
        return user_email
