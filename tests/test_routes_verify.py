import pytest
from httpx import AsyncClient, ASGITransport

AUTH = {"X-User-Email": "alice@example.com"}


@pytest.fixture
async def app():
    from tests.conftest import create_test_app
    return await create_test_app()


@pytest.mark.asyncio
async def test_verify_valid_key(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        create_r = await c.post("/api/keys", json={"name": "V"}, headers=AUTH)
        full_key = create_r.json()["key"]
        r = await c.post("/api/keys/verify", json={"key": full_key})
    assert r.status_code == 200
    assert r.json()["valid"] is True
    assert r.json()["user_email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_verify_invalid_key(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/keys/verify", json={"key": "sk-invalid"})
    assert r.status_code == 200
    assert r.json()["valid"] is False


@pytest.mark.asyncio
async def test_verify_revoked_key(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        create_r = await c.post("/api/keys", json={"name": "R"}, headers=AUTH)
        full_key = create_r.json()["key"]
        key_id = create_r.json()["id"]
        await c.delete(f"/api/keys/{key_id}", headers=AUTH)
        r = await c.post("/api/keys/verify", json={"key": full_key})
    assert r.status_code == 200
    assert r.json()["valid"] is False
