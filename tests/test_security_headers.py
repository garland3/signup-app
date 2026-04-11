import pytest
from httpx import AsyncClient, ASGITransport

from app.core.config import Settings
from app.core.rate_limit import limiter
from app.main import create_app


def _make_app(**overrides) -> "FastAPI":
    import app.core.config as config_mod
    defaults = dict(
        DEBUG_MODE=False,
        LITELLM_BASE_URL="http://mock:4000",
        LITELLM_ADMIN_KEY="sk-test",
        # Startup guard otherwise refuses to boot proxy-mode without a secret.
        FEATURE_PROXY_SECRET_ENABLED=False,
        ALLOW_INSECURE_STARTUP=True,
    )
    defaults.update(overrides)
    settings = Settings(**defaults)
    config_mod.settings = settings
    limiter.reset()
    return create_app()


@pytest.mark.asyncio
async def test_security_headers_present():
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/api/health")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in r.headers["permissions-policy"]
    # Strict CSP + isolation headers
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert r.headers["cross-origin-opener-policy"] == "same-origin"
    assert r.headers["cross-origin-resource-policy"] == "same-origin"


@pytest.mark.asyncio
async def test_hsts_present_when_not_debug():
    app = _make_app(DEBUG_MODE=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/api/health")
    assert "strict-transport-security" in r.headers
    assert "max-age=31536000" in r.headers["strict-transport-security"]


@pytest.mark.asyncio
async def test_hsts_absent_when_debug():
    app = _make_app(DEBUG_MODE=True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/api/health")
    assert "strict-transport-security" not in r.headers


@pytest.mark.asyncio
async def test_no_store_on_sensitive_api_routes():
    app = _make_app(ALLOW_TEST_USER=True, DEBUG_MODE=True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/api/me")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
