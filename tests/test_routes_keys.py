import pytest
from httpx import AsyncClient, ASGITransport

AUTH = {"X-User-Email": "alice@example.com"}


@pytest.fixture
async def app():
    from tests.conftest import create_test_app
    return await create_test_app()


@pytest.mark.asyncio
async def test_create_key(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/keys", json={"name": "My Key"}, headers=AUTH)
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "My Key"
    assert data["key"].startswith("sk-")
    assert len(data["key"]) == 51
    assert "prefix" in data
    assert "id" in data


@pytest.mark.asyncio
async def test_list_keys_masked(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/api/keys", json={"name": "Key 1"}, headers=AUTH)
        await c.post("/api/keys", json={"name": "Key 2"}, headers=AUTH)
        r = await c.get("/api/keys", headers=AUTH)
    assert r.status_code == 200
    keys = r.json()
    assert len(keys) == 2
    for k in keys:
        assert "key" not in k
        assert "prefix" in k
        assert "name" in k


@pytest.mark.asyncio
async def test_revoke_key(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        create_r = await c.post("/api/keys", json={"name": "Temp"}, headers=AUTH)
        key_id = create_r.json()["id"]
        r = await c.delete(f"/api/keys/{key_id}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["is_active"] is False


@pytest.mark.asyncio
async def test_update_key_name(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        create_r = await c.post("/api/keys", json={"name": "Old"}, headers=AUTH)
        key_id = create_r.json()["id"]
        r = await c.patch(f"/api/keys/{key_id}", json={"name": "New"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["name"] == "New"


@pytest.mark.asyncio
async def test_cannot_access_other_users_keys(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        create_r = await c.post(
            "/api/keys", json={"name": "Alice Key"}, headers=AUTH
        )
        key_id = create_r.json()["id"]
        r = await c.delete(
            f"/api/keys/{key_id}", headers={"X-User-Email": "bob@example.com"}
        )
    assert r.status_code == 404
