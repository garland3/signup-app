import pytest
import respx
from fastapi import FastAPI, Request
from httpx import AsyncClient, ASGITransport, Response
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import Settings
from app.core.middleware import AuthMiddleware
from app.routes.auth import router as auth_router


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
    # Patch module-level settings so /api/auth routes pick them up
    import app.core.config as config_mod
    config_mod.settings = settings

    app = FastAPI()
    app.add_middleware(AuthMiddleware, settings=settings)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.SESSION_SECRET,
        session_cookie=settings.SESSION_COOKIE_NAME,
        https_only=False,
    )
    app.include_router(auth_router)

    @app.get("/api/me")
    async def me(request: Request):
        return {"email": request.state.user_email}

    return app


@pytest.mark.asyncio
async def test_oauth_protected_route_without_session_returns_401():
    app = _make_app(_oauth_settings())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/api/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_oauth_browser_page_redirects_to_login():
    app = _make_app(_oauth_settings())

    @app.get("/dashboard")
    async def dashboard():
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/api/auth/login?next=/dashboard"


@pytest.mark.asyncio
async def test_oauth_login_redirects_to_provider():
    app = _make_app(_oauth_settings())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/api/auth/login", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://idp.example.com/authorize?")
    assert "client_id=client-id" in loc
    assert "response_type=code" in loc
    assert "state=" in loc


@pytest.mark.asyncio
@respx.mock
async def test_oauth_callback_sets_session_and_me_works():
    settings = _oauth_settings()
    app = _make_app(settings)

    respx.post("https://idp.example.com/token").mock(
        return_value=Response(200, json={"access_token": "at-123"})
    )
    respx.get("https://idp.example.com/userinfo").mock(
        return_value=Response(200, json={"email": "alice@example.com"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        login = await c.get("/api/auth/login", follow_redirects=False)
        state = login.headers["location"].split("state=")[1].split("&")[0]

        cb = await c.get(
            f"/api/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )
        assert cb.status_code == 302
        assert cb.headers["location"] == "/"

        r = await c.get("/api/me")
        assert r.status_code == 200
        assert r.json()["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_oauth_callback_rejects_bad_state():
    app = _make_app(_oauth_settings())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        await c.get("/api/auth/login", follow_redirects=False)
        r = await c.get(
            "/api/auth/callback?code=abc&state=wrong", follow_redirects=False
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_oauth_login_404_when_mode_is_proxy():
    settings = _oauth_settings(AUTH_MODE="proxy")
    app = _make_app(settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/api/auth/login", follow_redirects=False)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_oauth_logout_clears_session():
    settings = _oauth_settings()
    app = _make_app(settings)

    with respx.mock:
        respx.post("https://idp.example.com/token").mock(
            return_value=Response(200, json={"access_token": "at-123"})
        )
        respx.get("https://idp.example.com/userinfo").mock(
            return_value=Response(200, json={"email": "bob@example.com"})
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            login = await c.get("/api/auth/login", follow_redirects=False)
            state = login.headers["location"].split("state=")[1].split("&")[0]
            await c.get(
                f"/api/auth/callback?code=abc&state={state}",
                follow_redirects=False,
            )
            r = await c.get("/api/me")
            assert r.status_code == 200

            await c.get("/api/auth/logout", follow_redirects=False)
            r = await c.get("/api/me")
            assert r.status_code == 401
