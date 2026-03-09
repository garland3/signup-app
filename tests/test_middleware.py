import pytest
from fastapi import FastAPI, Request
from httpx import AsyncClient, ASGITransport

from app.core.config import Settings
from app.core.middleware import AuthMiddleware


def _make_app(settings: Settings) -> FastAPI:
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
    s = Settings(DATABASE_URL="sqlite+aiosqlite:///test.db", DEBUG_MODE=False)
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_protected_route_no_header_returns_401():
    s = Settings(DATABASE_URL="sqlite+aiosqlite:///test.db", DEBUG_MODE=False)
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_protected_route_with_header():
    s = Settings(DATABASE_URL="sqlite+aiosqlite:///test.db", DEBUG_MODE=False)
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me", headers={"X-User-Email": "alice@example.com"})
    assert r.status_code == 200
    assert r.json()["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_debug_mode_fallback():
    s = Settings(
        DATABASE_URL="sqlite+aiosqlite:///test.db",
        DEBUG_MODE=True,
        TEST_USER="debug@test.com",
    )
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me")
    assert r.status_code == 200
    assert r.json()["email"] == "debug@test.com"


@pytest.mark.asyncio
async def test_proxy_secret_required():
    s = Settings(
        DATABASE_URL="sqlite+aiosqlite:///test.db",
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
