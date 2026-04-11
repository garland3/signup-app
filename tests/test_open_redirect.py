import pytest
import respx
from httpx import AsyncClient, ASGITransport, Response

from app.core.config import Settings
from app.core.middleware import AuthMiddleware
from app.core.rate_limit import limiter
from app.core.sessions import InMemorySessionMiddleware
from app.routes.auth import router as auth_router
from fastapi import FastAPI, Request


def _oauth_settings(**overrides) -> Settings:
    base = dict(
        AUTH_MODE="oauth",
        OAUTH_CLIENT_ID="client-id",
        OAUTH_CLIENT_SECRET="client-secret",
        OAUTH_AUTHORIZE_URL="https://idp.example.com/authorize",
        OAUTH_TOKEN_URL="https://idp.example.com/token",
        OAUTH_USERINFO_URL="https://idp.example.com/userinfo",
        OAUTH_REDIRECT_URL="http://test/api/auth/callback",
        OAUTH_SCOPES="openid email",
        OAUTH_EMAIL_FIELD="email",
        SESSION_SECRET="test-secret-do-not-use-in-prod",
        DEBUG_MODE=False,
    )
    base.update(overrides)
    return Settings(**base)


def _make_app(settings: Settings) -> FastAPI:
    import app.core.config as config_mod
    config_mod.settings = settings
    limiter.reset()

    app = FastAPI()
    app.add_middleware(AuthMiddleware, settings=settings)
    app.add_middleware(
        InMemorySessionMiddleware,
        cookie_name=settings.SESSION_COOKIE_NAME,
        max_age=settings.SESSION_MAX_AGE,
        idle_timeout=settings.SESSION_IDLE_TIMEOUT,
        https_only=False,
    )
    app.include_router(auth_router)

    @app.get("/api/me")
    async def me(request: Request):
        return {"email": request.state.user_email}

    return app


async def _do_oauth_flow(c: AsyncClient, next_url: str) -> str:
    """Perform a full OAuth flow with the given next_url and return the final redirect location."""
    login = await c.get(
        f"/api/auth/login?next={next_url}", follow_redirects=False
    )
    state = login.headers["location"].split("state=")[1].split("&")[0]

    with respx.mock:
        respx.post("https://idp.example.com/token").mock(
            return_value=Response(200, json={"access_token": "at-123"})
        )
        respx.get("https://idp.example.com/userinfo").mock(
            return_value=Response(200, json={"email": "alice@example.com"})
        )
        cb = await c.get(
            f"/api/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )
    return cb.headers["location"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malicious_next",
    [
        "//attacker.com",
        "///attacker.com",
        "//attacker.com/phishing",
        "/\\attacker.com",
        "/%2F/attacker.com",
        "/%2f%2fattacker.com",
        "https://attacker.com",
        "http://attacker.com",
        "javascript:alert(1)",
    ],
)
async def test_open_redirect_blocked(malicious_next):
    app = _make_app(_oauth_settings())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        location = await _do_oauth_flow(c, malicious_next)
    assert location == "/", f"Expected '/' but got '{location}' for next={malicious_next}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "safe_next,expected",
    [
        ("/", "/"),
        ("/dashboard", "/dashboard"),
        ("/settings?tab=keys", "/settings?tab=keys"),
    ],
)
async def test_safe_redirects_allowed(safe_next, expected):
    app = _make_app(_oauth_settings())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        location = await _do_oauth_flow(c, safe_next)
    assert location == expected
