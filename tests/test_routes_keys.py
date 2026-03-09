import pytest
import respx
from httpx import AsyncClient, ASGITransport, Response

AUTH = {"X-User-Email": "alice@example.com"}
LITELLM = "http://mock-litellm:4000"


@pytest.fixture
def app():
    from tests.conftest import create_test_app
    return create_test_app()


@pytest.mark.asyncio
@respx.mock
async def test_create_key(app):
    respx.post(f"{LITELLM}/key/generate").mock(
        return_value=Response(200, json={
            "key": "sk-test1234567890abcdef1234567890abcdef1234567890ab",
            "token_id": "tok_abc123",
            "key_alias": "My Key",
            "user_id": "alice@example.com",
            "created_at": "2026-03-09T00:00:00Z",
            "expires": None,
            "spend": 0,
            "max_budget": None,
            "models": [],
            "blocked": False,
        })
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/keys", json={"name": "My Key"}, headers=AUTH)

    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "My Key"
    assert data["key"].startswith("sk-")
    assert "prefix" in data
    assert "id" in data


@pytest.mark.asyncio
@respx.mock
async def test_list_keys(app):
    respx.get(f"{LITELLM}/key/list").mock(
        return_value=Response(200, json=[
            {
                "token_id": "tok_1",
                "token": "sk-abc12345...",
                "key_alias": "Key 1",
                "user_id": "alice@example.com",
                "created_at": "2026-03-09T00:00:00Z",
                "spend": 1.50,
                "blocked": False,
            },
            {
                "token_id": "tok_2",
                "token": "sk-def67890...",
                "key_alias": "Key 2",
                "user_id": "alice@example.com",
                "created_at": "2026-03-08T00:00:00Z",
                "spend": 0,
                "blocked": False,
            },
        ])
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/keys", headers=AUTH)

    assert r.status_code == 200
    keys = r.json()
    assert len(keys) == 2
    for k in keys:
        assert "key" not in k  # Full key must never appear in list
        assert "prefix" in k
        assert "name" in k


@pytest.mark.asyncio
@respx.mock
async def test_delete_key(app):
    respx.post(f"{LITELLM}/key/delete").mock(
        return_value=Response(200, json={"deleted_keys": ["sk-abc123"]})
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.delete("/api/keys/sk-abc123", headers=AUTH)

    assert r.status_code == 200


@pytest.mark.asyncio
@respx.mock
async def test_update_key(app):
    respx.post(f"{LITELLM}/key/update").mock(
        return_value=Response(200, json={"key": "sk-abc123", "key_alias": "New Name"})
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.patch(
            "/api/keys/sk-abc123",
            json={"key_alias": "New Name"},
            headers=AUTH,
        )

    assert r.status_code == 200


@pytest.mark.asyncio
@respx.mock
async def test_create_key_with_options(app):
    respx.post(f"{LITELLM}/key/generate").mock(
        return_value=Response(200, json={
            "key": "sk-test1234567890abcdef1234567890abcdef1234567890ab",
            "token_id": "tok_xyz",
            "key_alias": "Budget Key",
            "user_id": "alice@example.com",
            "created_at": "2026-03-09T00:00:00Z",
            "expires": "2026-06-09T00:00:00Z",
            "spend": 0,
            "max_budget": 100.0,
            "models": ["gpt-4"],
            "blocked": False,
        })
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/keys", json={
            "name": "Budget Key",
            "duration": "90d",
            "max_budget": 100.0,
            "models": ["gpt-4"],
        }, headers=AUTH)

    assert r.status_code == 201
    data = r.json()
    assert data["max_budget"] == 100.0


@pytest.mark.asyncio
@respx.mock
async def test_litellm_error_returns_502(app):
    respx.get(f"{LITELLM}/key/list").mock(
        return_value=Response(500, json={"error": "Internal Server Error"})
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/keys", headers=AUTH)

    assert r.status_code == 502
