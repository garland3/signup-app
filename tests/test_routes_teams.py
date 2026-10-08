import json

import pytest
import respx
from httpx import AsyncClient, ASGITransport, Response

AUTH = {"X-User-Email": "alice@example.com"}
LITELLM = "http://mock-litellm:4000"

ALPHA = {"team_id": "team-alpha", "team_alias": "Alpha Team"}
BETA = {"team_id": "team-beta", "team_alias": "Beta Team"}


@pytest.fixture
def app():
    from tests.conftest import create_test_app
    return create_test_app(teams_enabled=True)


@pytest.fixture
def app_no_teams():
    from tests.conftest import create_test_app
    return create_test_app(teams_enabled=False)


@pytest.mark.asyncio
@respx.mock
async def test_available_excludes_joined(app):
    # available (what the proxy offers) wrapped in {"available_teams": ...};
    # list (already joined) as a bare array -- both shapes are normalized.
    respx.get(f"{LITELLM}/team/available").mock(
        return_value=Response(200, json={"available_teams": [ALPHA, BETA]})
    )
    respx.get(f"{LITELLM}/team/list").mock(
        return_value=Response(200, json=[ALPHA])
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/teams/available", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert [t["team_id"] for t in data] == ["team-beta"]
    assert data[0]["team_alias"] == "Beta Team"


@pytest.mark.asyncio
@respx.mock
async def test_join_team_success_targets_caller(app):
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=[]))
    respx.get(f"{LITELLM}/team/available").mock(
        return_value=Response(200, json={"available_teams": [ALPHA]})
    )
    # Existing user -> ensure_user does not create.
    respx.get(f"{LITELLM}/user/info").mock(
        return_value=Response(200, json={"user_id": "alice@example.com"})
    )
    member_route = respx.post(f"{LITELLM}/team/member_add").mock(
        return_value=Response(200, json={"team_id": "team-alpha"})
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/teams/join", json={"team_id": "team-alpha"}, headers=AUTH)

    assert r.status_code == 200
    assert r.json()["team_id"] == "team-alpha"
    sent = json.loads(member_route.calls.last.request.content)
    assert sent["team_id"] == "team-alpha"
    # The target is always the caller's own email, never client-supplied.
    assert sent["member"]["user_email"] == "alice@example.com"
    assert sent["member"]["role"] == "user"


@pytest.mark.asyncio
@respx.mock
async def test_join_team_auto_creates_user(app):
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=[]))
    respx.get(f"{LITELLM}/team/available").mock(
        return_value=Response(200, json={"available_teams": [ALPHA]})
    )
    # User does not exist yet -> ensure_user creates it.
    respx.get(f"{LITELLM}/user/info").mock(
        return_value=Response(404, json={"detail": "User not found"})
    )
    new_user = respx.post(f"{LITELLM}/user/new").mock(
        return_value=Response(200, json={"user_id": "alice@example.com"})
    )
    respx.post(f"{LITELLM}/team/member_add").mock(
        return_value=Response(200, json={"team_id": "team-alpha"})
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/teams/join", json={"team_id": "team-alpha"}, headers=AUTH)

    assert r.status_code == 200
    assert new_user.called


@pytest.mark.asyncio
@respx.mock
async def test_join_team_not_available_403(app):
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=[]))
    respx.get(f"{LITELLM}/team/available").mock(
        return_value=Response(200, json={"available_teams": [BETA]})
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/teams/join", json={"team_id": "team-alpha"}, headers=AUTH)
    assert r.status_code == 403


@pytest.mark.asyncio
@respx.mock
async def test_join_team_already_member_409(app):
    # Already joined -> short-circuits to 409 before touching member_add.
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=[ALPHA]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/teams/join", json={"team_id": "team-alpha"}, headers=AUTH)
    assert r.status_code == 409


@pytest.mark.asyncio
@respx.mock
async def test_join_team_upstream_error_502(app):
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=[]))
    respx.get(f"{LITELLM}/team/available").mock(
        return_value=Response(200, json={"available_teams": [ALPHA]})
    )
    respx.get(f"{LITELLM}/user/info").mock(
        return_value=Response(200, json={"user_id": "alice@example.com"})
    )
    respx.post(f"{LITELLM}/team/member_add").mock(
        return_value=Response(500, json={"error": "boom"})
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/teams/join", json={"team_id": "team-alpha"}, headers=AUTH)
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_teams_routes_404_when_disabled(app_no_teams):
    async with AsyncClient(
        transport=ASGITransport(app=app_no_teams), base_url="http://test"
    ) as c:
        r1 = await c.get("/api/teams/available", headers=AUTH)
        r2 = await c.post("/api/teams/join", json={"team_id": "team-alpha"}, headers=AUTH)
    assert r1.status_code == 404
    assert r2.status_code == 404
