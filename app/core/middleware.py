import hmac
from urllib.parse import quote, urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from app.core.audit import audit
from app.core.config import Settings
from app.core.rate_limit import limiter

PUBLIC_PATHS = {"/api/health"}
PUBLIC_PREFIXES = ("/api/auth/",)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next):
        full_path = request.url.path

        # Strip the configured URL prefix (if any) so path-based policy
        # checks can operate on the app-relative path.
        root_path = self.settings.normalized_root_path
        if root_path and full_path.startswith(root_path):
            path = full_path[len(root_path):] or "/"
        else:
            path = full_path

        # Allow public paths
        if path in PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)

        if any(path.startswith(p) for p in PUBLIC_PREFIXES):
            # Auth endpoints still get rate limiting on login / callback
            # so brute-force attempts can't run unchecked.
            if path in {"/api/auth/login", "/api/auth/callback"}:
                ident = request.client.host if request.client else "unknown"
                if not limiter.check(
                    "login",
                    ident,
                    self.settings.RATE_LIMIT_LOGIN_PER_MINUTE,
                    60,
                ):
                    audit("rate_limited", bucket="login", ident=ident, path=path)
                    return JSONResponse(
                        {"detail": "Too many requests"}, status_code=429
                    )
            return await call_next(request)

        is_api = path.startswith("/api")

        if self.settings.AUTH_MODE == "oauth":
            user_email = self._resolve_oauth_user(request)
            if not user_email:
                # For browser navigation (non-API GETs), kick off the OAuth
                # flow instead of returning a bare 401. API calls still get
                # 401 so the JS client can detect and handle it.
                if not is_api and request.method == "GET":
                    next_url = full_path
                    if request.url.query:
                        next_url = f"{next_url}?{request.url.query}"
                    return RedirectResponse(
                        f"{root_path}/api/auth/login?next={quote(next_url, safe='/?&=')}",
                        status_code=302,
                    )
                return JSONResponse(
                    {"detail": "Unauthorized"}, status_code=401
                )
            # CSRF: for OAuth (cookie-authenticated) mode, verify the
            # request Origin/Referer on state-changing API calls. This
            # complements SameSite=lax on the session cookie.
            if is_api and request.method not in SAFE_METHODS:
                csrf_error = self._check_csrf(request)
                if csrf_error is not None:
                    audit(
                        "csrf_denied",
                        path=path,
                        method=request.method,
                        user=user_email,
                    )
                    return csrf_error
        else:
            # Proxy mode only guards API routes; static pages pass through.
            if not is_api:
                return await call_next(request)
            user_email = self._resolve_proxy_user(request)
            if isinstance(user_email, JSONResponse):
                return user_email
            if not user_email:
                return JSONResponse(
                    {"detail": "Unauthorized"}, status_code=401
                )

        if self.settings.STRIP_USER_DOMAIN and "@" in user_email:
            user_email = user_email.split("@", 1)[0]

        # Per-user API rate limit. Apply after user resolution so each
        # user gets their own bucket; unauthenticated requests never
        # reach here.
        if is_api and not limiter.check(
            "api",
            user_email,
            self.settings.RATE_LIMIT_API_PER_MINUTE,
            60,
        ):
            audit("rate_limited", bucket="api", user=user_email, path=path)
            return JSONResponse(
                {"detail": "Too many requests"}, status_code=429
            )

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

        # Dev-only fallback: only honoured when ALLOW_TEST_USER is set
        # explicitly. Decoupled from DEBUG_MODE so a single misconfigured
        # env var cannot silently disable auth.
        if not user_email and self.settings.ALLOW_TEST_USER:
            user_email = self.settings.TEST_USER

        return user_email

    def _resolve_oauth_user(self, request: Request):
        session = getattr(request, "session", None)
        if session is None:
            return None
        user_email = session.get("user_email")
        if not user_email and self.settings.ALLOW_TEST_USER:
            user_email = self.settings.TEST_USER
        return user_email

    def _check_csrf(self, request: Request) -> JSONResponse | None:
        """Reject cross-origin state-changing requests in OAuth mode.

        Uses Origin (preferred) or Referer. Same-origin is enforced by
        comparing scheme+host+port against the request's own URL.
        """
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if not origin and not referer:
            # No Origin and no Referer at all - refuse.
            return JSONResponse(
                {"detail": "Missing Origin/Referer"}, status_code=403
            )

        target = urlparse(str(request.url))
        source_url = origin or referer
        source = urlparse(source_url)
        if (source.scheme, source.hostname, source.port) != (
            target.scheme,
            target.hostname,
            target.port,
        ):
            return JSONResponse(
                {"detail": "Cross-origin request denied"}, status_code=403
            )
        return None
