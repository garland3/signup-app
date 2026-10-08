import pytest
import respx
from httpx import AsyncClient, ASGITransport, Response

LITELLM = "http://mock-litellm:4000"
AUTH = {"X-User-Email": "alice@example.com"}


@pytest.mark.asyncio
async def test_me_returns_user_email():
    from tests.conftest import create_test_app
    app = create_test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_me_omits_teams_when_feature_disabled():
    from tests.conftest import create_test_app
    app = create_test_app()  # teams feature off by default
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me", headers=AUTH)
    assert r.status_code == 200
    assert "teams" not in r.json()


@pytest.mark.asyncio
@respx.mock
async def test_me_includes_teams_when_feature_enabled():
    from tests.conftest import create_test_app
    app = create_test_app(teams_enabled=True)
    respx.get(f"{LITELLM}/team/list").mock(
        return_value=Response(
            200, json=[{"team_id": "team-alpha", "team_alias": "Alpha Team"}]
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["teams"] == [{"team_id": "team-alpha", "team_alias": "Alpha Team"}]


@pytest.mark.asyncio
@respx.mock
async def test_me_teams_degrades_to_empty_on_upstream_error():
    from tests.conftest import create_test_app
    app = create_test_app(teams_enabled=True)
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(500, json={}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me", headers=AUTH)
    # /api/me must stay resilient even if the teams lookup fails.
    assert r.status_code == 200
    assert r.json()["teams"] == []
