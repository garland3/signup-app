import pytest
import respx
from fastapi import FastAPI, Request
from httpx import AsyncClient, ASGITransport, Response

from app.core.config import Settings
from app.core.middleware import AuthMiddleware
from app.core.rate_limit import limiter
from app.core.sessions import InMemorySessionMiddleware
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
@respx.mock
async def test_oauth_csrf_same_origin_write_allowed():
    # In the default (no TRUSTED_ORIGINS) configuration, a write from
    # the same origin as request.url must succeed.
    settings = _oauth_settings()
    app = _make_app(settings)

    @app.post("/api/widgets")
    async def create_widget(request: Request):
        return {"ok": True, "user": request.state.user_email}

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
        await c.get(
            f"/api/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )
        r = await c.post(
            "/api/widgets", headers={"Origin": "http://test"}
        )
        assert r.status_code == 200


@pytest.mark.asyncio
@respx.mock
async def test_oauth_csrf_cross_origin_write_denied_by_default():
    # Without TRUSTED_ORIGINS, a write with an Origin that doesn't
    # match the app's own URL must be rejected.
    settings = _oauth_settings()
    app = _make_app(settings)

    @app.post("/api/widgets")
    async def create_widget(request: Request):  # pragma: no cover
        return {"ok": True}

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
        await c.get(
            f"/api/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )
        r = await c.post(
            "/api/widgets",
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403
        assert r.json()["detail"] == "Cross-origin request denied"


@pytest.mark.asyncio
@respx.mock
async def test_oauth_csrf_trusted_origin_allowed_behind_proxy():
    # Behind a TLS-terminating ingress the app sees http://test but the
    # browser's Origin header is the public https URL. With
    # TRUSTED_ORIGINS configured, the write must succeed.
    settings = _oauth_settings(
        TRUSTED_ORIGINS="https://app.example.com,https://staging.example.com"
    )
    app = _make_app(settings)

    @app.post("/api/widgets")
    async def create_widget(request: Request):
        return {"ok": True, "user": request.state.user_email}

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
        await c.get(
            f"/api/auth/callback?code=abc&state={state}",
            follow_redirects=False,
        )

        # Origin matches an entry in TRUSTED_ORIGINS - allowed.
        r = await c.post(
            "/api/widgets",
            headers={"Origin": "https://app.example.com"},
        )
        assert r.status_code == 200

        # Origin not in TRUSTED_ORIGINS - denied, even though same host
        # as the app would otherwise have worked.
        r = await c.post(
            "/api/widgets",
            headers={"Origin": "http://test"},
        )
        assert r.status_code == 403

        # Attacker origin still denied.
        r = await c.post(
            "/api/widgets",
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403


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

            # Logout is POST-only (GET used to be a CSRF-able side effect).
            get_logout = await c.get("/api/auth/logout", follow_redirects=False)
            assert get_logout.status_code == 405

            await c.post("/api/auth/logout", follow_redirects=False)
            r = await c.get("/api/me")
            assert r.status_code == 401
