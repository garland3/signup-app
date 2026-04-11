import pytest
from fastapi import FastAPI, Request
from httpx import AsyncClient, ASGITransport

from app.core.config import Settings
from app.core.middleware import AuthMiddleware
from app.core.rate_limit import limiter


def _make_app(settings: Settings) -> FastAPI:
    # Reset rate limit buckets so one test's counts don't spill into another.
    limiter.reset()
    app = FastAPI()
    app.add_middleware(AuthMiddleware, settings=settings)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/me")
    async def me(request: Request):
        return {"email": request.state.user_email}

    return app


@pytest.mark.asyncio
async def test_health_no_auth_required():
    s = Settings(
        DEBUG_MODE=False,
        FEATURE_PROXY_SECRET_ENABLED=False,
    )
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_protected_route_no_header_returns_401():
    s = Settings(
        DEBUG_MODE=False,
        FEATURE_PROXY_SECRET_ENABLED=False,
    )
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_protected_route_with_header():
    s = Settings(
        DEBUG_MODE=False,
        FEATURE_PROXY_SECRET_ENABLED=False,
    )
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me", headers={"X-User-Email": "alice@example.com"})
    assert r.status_code == 200
    assert r.json()["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_allow_test_user_fallback():
    # Test-user fallback is opt-in via ALLOW_TEST_USER, decoupled from DEBUG_MODE.
    s = Settings(
        DEBUG_MODE=True,
        ALLOW_TEST_USER=True,
        FEATURE_PROXY_SECRET_ENABLED=False,
        TEST_USER="debug@test.com",
    )
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me")
    assert r.status_code == 200
    assert r.json()["email"] == "debug@test.com"


@pytest.mark.asyncio
async def test_debug_mode_alone_does_not_bypass_auth():
    # DEBUG_MODE=true without ALLOW_TEST_USER must NOT let unauthenticated
    # requests through. The two flags are intentionally decoupled.
    s = Settings(
        DEBUG_MODE=True,
        FEATURE_PROXY_SECRET_ENABLED=False,
        TEST_USER="debug@test.com",
    )
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_proxy_secret_required():
    s = Settings(
        DEBUG_MODE=False,
        FEATURE_PROXY_SECRET_ENABLED=True,
        PROXY_SECRET="mysecret",
    )
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me", headers={"X-User-Email": "a@b.com"})
        assert r.status_code == 401

        r = await c.get(
            "/api/me",
            headers={"X-User-Email": "a@b.com", "X-Proxy-Secret": "wrong"},
        )
        assert r.status_code == 401

        r = await c.get(
            "/api/me",
            headers={"X-User-Email": "a@b.com", "X-Proxy-Secret": "mysecret"},
        )
        assert r.status_code == 200
