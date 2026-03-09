import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import Settings

PUBLIC_PATHS = {"/api/health", "/api/keys/verify"}


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Allow public paths
        if path in PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)

        # Skip non-API paths (serve frontend)
        if not path.startswith("/api"):
            return await call_next(request)

        # Proxy secret check (production only)
        if (
            self.settings.FEATURE_PROXY_SECRET_ENABLED
            and not self.settings.DEBUG_MODE
        ):
            proxy_secret = request.headers.get(self.settings.PROXY_SECRET_HEADER, "")
            if not hmac.compare_digest(proxy_secret, self.settings.PROXY_SECRET):
                return JSONResponse(
                    {"detail": "Unauthorized"}, status_code=401
                )

        # Extract user email from header
        user_email = request.headers.get(self.settings.AUTH_USER_HEADER)

        # Debug mode fallback
        if not user_email and self.settings.DEBUG_MODE:
            user_email = self.settings.TEST_USER

        if not user_email:
            return JSONResponse(
                {"detail": "Unauthorized"}, status_code=401
            )

        request.state.user_email = user_email
        return await call_next(request)
