import pytest
from httpx import AsyncClient, ASGITransport

from app.core.config import Settings
import app.core.config as config_mod
import app.main as main_mod


def _install_settings(**overrides) -> Settings:
    base = dict(
        DEBUG_MODE=True,
        LITELLM_BASE_URL="http://mock-litellm:4000",
        LITELLM_ADMIN_KEY="sk-test-admin-key",
    )
    base.update(overrides)
    settings = Settings(**base)
    config_mod.settings = settings
    return settings


@pytest.mark.asyncio
async def test_no_root_path_serves_from_site_root():
    _install_settings(ROOT_PATH="")
    app = main_mod.create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

        r = await c.get("/")
        assert r.status_code == 200
        # Placeholder should be replaced with empty string, leaving absolute paths.
        assert "/static/app.js" in r.text
        assert 'window.APP_ROOT_PATH = "";' in r.text


@pytest.mark.asyncio
async def test_root_path_mounts_app_under_prefix():
    _install_settings(ROOT_PATH="/start")
    app = main_mod.create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        # Health endpoint is only reachable beneath the prefix.
        r = await c.get("/start/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

        # The container root redirects to the prefix.
        r = await c.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/start/"

        # Frontend is served at the prefix with assets pointing at it.
        r = await c.get("/start/")
        assert r.status_code == 200
        assert "/start/static/app.js" in r.text
        assert "/start/static/style.css" in r.text
        assert 'window.APP_ROOT_PATH = "/start";' in r.text

        # Static assets are reachable at the prefixed path.
        r = await c.get("/start/static/app.js")
        assert r.status_code == 200
        assert "API_BASE" in r.text


@pytest.mark.asyncio
async def test_root_path_bare_paths_return_404():
    _install_settings(ROOT_PATH="/start")
    app = main_mod.create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        # Without the prefix, API routes should not be found.
        r = await c.get("/api/health")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_root_path_normalization_variants():
    for raw in ("start", "/start", "/start/", " /start "):
        s = Settings(
            DEBUG_MODE=True,
            LITELLM_BASE_URL="http://mock-litellm:4000",
            LITELLM_ADMIN_KEY="sk-test-admin-key",
            ROOT_PATH=raw,
        )
        assert s.normalized_root_path == "/start", raw


@pytest.mark.asyncio
async def test_root_path_proxy_auth_middleware_honours_prefix():
    _install_settings(
        ROOT_PATH="/start",
        DEBUG_MODE=False,
        AUTH_MODE="proxy",
    )
    app = main_mod.create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        # Unauthenticated API call at the prefix returns 401.
        r = await c.get("/start/api/me")
        assert r.status_code == 401

        # With the auth header injected, the user is resolved.
        r = await c.get(
            "/start/api/me", headers={"X-User-Email": "alice@example.com"}
        )
        assert r.status_code == 200
        assert r.json()["email"] == "alice@example.com"
