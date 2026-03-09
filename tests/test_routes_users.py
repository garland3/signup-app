import pytest
from httpx import AsyncClient, ASGITransport


@pytest.mark.asyncio
async def test_me_returns_user_email():
    from tests.conftest import create_test_app
    app = await create_test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me", headers={"X-User-Email": "alice@example.com"})
    assert r.status_code == 200
    data = r.json()
    assert data["email"] == "alice@example.com"
    assert "id" in data
