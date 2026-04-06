import pytest
import respx
from httpx import AsyncClient, ASGITransport, Response

AUTH = {"X-User-Email": "alice@example.com"}
LITELLM = "http://mock-litellm:4000"


def mock_ensure_user():
    """Mock the GET /user/info + POST /user/new calls used by ensure_user."""
    respx.get(f"{LITELLM}/user/info").mock(
        return_value=Response(404, json={"detail": "User not found"})
    )
    respx.post(f"{LITELLM}/user/new").mock(
        return_value=Response(200, json={"user_id": "alice@example.com"})
    )


@pytest.fixture
def app():
    from tests.conftest import create_test_app
    return create_test_app()


@pytest.mark.asyncio
@respx.mock
async def test_create_key(app):
    mock_ensure_user()
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
async def test_delete_key_with_ownership_check(app):
    # get_key_info returns key owned by alice
    respx.get(f"{LITELLM}/key/info").mock(
        return_value=Response(200, json={
            "token_id": "tok_abc123",
            "token": "sk-abc123fullkey",
            "key_alias": "Alice Key",
            "user_id": "alice@example.com",
            "blocked": False,
        })
    )
    respx.post(f"{LITELLM}/key/update").mock(
        return_value=Response(200, json={"token_id": "tok_abc123"})
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.delete("/api/keys/tok_abc123", headers=AUTH)

    assert r.status_code == 200
    assert r.json() == {"deleted": True}


@pytest.mark.asyncio
@respx.mock
async def test_delete_key_denied_for_other_user(app):
    # get_key_info returns key owned by bob, not alice
    respx.get(f"{LITELLM}/key/info").mock(
        return_value=Response(200, json={
            "token_id": "tok_bob123",
            "token": "sk-bobkey",
            "key_alias": "Bob Key",
            "user_id": "bob@example.com",
            "blocked": False,
        })
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.delete("/api/keys/tok_bob123", headers=AUTH)

    assert r.status_code == 404


@pytest.mark.asyncio
@respx.mock
async def test_update_key_with_ownership_check(app):
    respx.get(f"{LITELLM}/key/info").mock(
        return_value=Response(200, json={
            "token_id": "tok_abc123",
            "token": "sk-abc123fullkey",
            "key_alias": "Old Name",
            "user_id": "alice@example.com",
            "blocked": False,
        })
    )
    respx.post(f"{LITELLM}/key/update").mock(
        return_value=Response(200, json={
            "token_id": "tok_abc123",
            "token": "sk-abc123fullkey",
            "key": "sk-abc123fullkey",
            "key_alias": "New Name",
            "user_id": "alice@example.com",
            "blocked": False,
            "created_at": "2026-03-09T00:00:00Z",
            "spend": 0,
        })
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.patch(
            "/api/keys/tok_abc123",
            json={"key_alias": "New Name"},
            headers=AUTH,
        )

    assert r.status_code == 200
    data = r.json()
    # Must not leak full key
    assert "key" not in data
    assert data["name"] == "New Name"
    assert "prefix" in data


@pytest.mark.asyncio
@respx.mock
async def test_update_key_denied_for_other_user(app):
    respx.get(f"{LITELLM}/key/info").mock(
        return_value=Response(200, json={
            "token_id": "tok_bob123",
            "token": "sk-bobkey",
            "key_alias": "Bob Key",
            "user_id": "bob@example.com",
        })
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.patch(
            "/api/keys/tok_bob123",
            json={"key_alias": "Hacked"},
            headers=AUTH,
        )

    assert r.status_code == 404


@pytest.mark.asyncio
@respx.mock
async def test_block_key_sanitizes_response(app):
    respx.get(f"{LITELLM}/key/info").mock(
        return_value=Response(200, json={
            "token_id": "tok_abc123",
            "token": "sk-abc123fullkey",
            "key": "sk-abc123fullkey",
            "key_alias": "My Key",
            "user_id": "alice@example.com",
            "blocked": False,
            "created_at": "2026-03-09T00:00:00Z",
            "spend": 0,
        })
    )
    respx.post(f"{LITELLM}/key/block").mock(
        return_value=Response(200, json={
            "token_id": "tok_abc123",
            "token": "sk-abc123fullkey",
            "key": "sk-abc123fullkey",
            "key_alias": "My Key",
            "user_id": "alice@example.com",
            "blocked": True,
            "created_at": "2026-03-09T00:00:00Z",
            "spend": 0,
        })
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/keys/tok_abc123/block", headers=AUTH)

    assert r.status_code == 200
    data = r.json()
    # Must not leak full key
    assert "key" not in data
    assert data["is_active"] is False


@pytest.mark.asyncio
@respx.mock
async def test_block_key_denied_for_other_user(app):
    respx.get(f"{LITELLM}/key/info").mock(
        return_value=Response(200, json={
            "token_id": "tok_bob123",
            "token": "sk-bobkey",
            "key_alias": "Bob Key",
            "user_id": "bob@example.com",
        })
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/keys/tok_bob123/block", headers=AUTH)

    assert r.status_code == 404


@pytest.mark.asyncio
@respx.mock
async def test_create_key_with_options(app):
    mock_ensure_user()
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


@pytest.mark.asyncio
async def test_get_config(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/config", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert "app_name" in data
    assert "required_metadata" in data
    assert "max_active_keys" in data


@pytest.mark.asyncio
@respx.mock
async def test_soft_delete_calls_update_with_zero_duration(app):
    respx.get(f"{LITELLM}/key/info").mock(
        return_value=Response(200, json={
            "token_id": "tok_abc123",
            "token": "sk-abc123fullkey",
            "key_alias": "My Key",
            "user_id": "alice@example.com",
            "blocked": False,
            "metadata": {"duration": "30d", "project": "p1"},
        })
    )
    update_route = respx.post(f"{LITELLM}/key/update").mock(
        return_value=Response(200, json={"token_id": "tok_abc123"})
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.delete("/api/keys/tok_abc123", headers=AUTH)

    assert r.status_code == 200
    assert r.json() == {"deleted": True}
    assert update_route.called
    import json
    sent = json.loads(update_route.calls[0].request.content)
    assert sent["duration"] == "0s"
    assert sent["metadata"]["duration"] == "0s"
    assert sent["metadata"]["project"] == "p1"


@pytest.mark.asyncio
@respx.mock
async def test_create_key_rejected_when_required_metadata_missing(app):
    from app.core.config import get_settings
    s = get_settings()
    original = s.REQUIRED_KEY_METADATA
    s.REQUIRED_KEY_METADATA = "project,task_number"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/keys", json={"name": "X"}, headers=AUTH)
        assert r.status_code == 400
        assert "project" in r.json()["detail"]
    finally:
        s.REQUIRED_KEY_METADATA = original


@pytest.mark.asyncio
@respx.mock
async def test_create_key_with_metadata_stored(app):
    from app.core.config import get_settings
    s = get_settings()
    original = s.REQUIRED_KEY_METADATA
    s.REQUIRED_KEY_METADATA = "project,task_number"

    mock_ensure_user()
    generate_route = respx.post(f"{LITELLM}/key/generate").mock(
        return_value=Response(200, json={
            "key": "sk-test1234567890abcdef",
            "token_id": "tok_m",
            "key_alias": "Meta Key",
            "user_id": "alice@example.com",
            "created_at": "2026-03-09T00:00:00Z",
            "metadata": {"project": "phoenix", "task_number": "T-42"},
        })
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/keys", json={
                "name": "Meta Key",
                "metadata": {"project": "phoenix", "task_number": "T-42"},
            }, headers=AUTH)
        assert r.status_code == 201
        data = r.json()
        assert data["metadata"]["project"] == "phoenix"
        assert data["metadata"]["task_number"] == "T-42"
        import json
        sent = json.loads(generate_route.calls[0].request.content)
        assert sent["metadata"]["project"] == "phoenix"
    finally:
        s.REQUIRED_KEY_METADATA = original


@pytest.mark.asyncio
@respx.mock
async def test_max_active_keys_enforced(app):
    from app.core.config import get_settings
    s = get_settings()
    original = s.MAX_ACTIVE_KEYS_PER_USER
    s.MAX_ACTIVE_KEYS_PER_USER = 2

    mock_ensure_user()
    respx.get(f"{LITELLM}/key/list").mock(
        return_value=Response(200, json=[
            {"token_id": "t1", "token": "sk-1", "blocked": False, "user_id": "alice@example.com"},
            {"token_id": "t2", "token": "sk-2", "blocked": False, "user_id": "alice@example.com"},
        ])
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/keys", json={"name": "X"}, headers=AUTH)
        assert r.status_code == 400
        assert "limit" in r.json()["detail"].lower()
    finally:
        s.MAX_ACTIVE_KEYS_PER_USER = original


@pytest.mark.asyncio
@respx.mock
async def test_list_keys_sorts_active_first(app):
    respx.get(f"{LITELLM}/key/list").mock(
        return_value=Response(200, json=[
            {"token_id": "t1", "token": "sk-1", "blocked": True, "user_id": "alice@example.com", "created_at": "2026-03-09T00:00:00Z"},
            {"token_id": "t2", "token": "sk-2", "blocked": False, "user_id": "alice@example.com", "created_at": "2026-03-08T00:00:00Z"},
            {"token_id": "t3", "token": "sk-3", "blocked": False, "user_id": "alice@example.com", "created_at": "2026-03-10T00:00:00Z"},
        ])
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/keys", headers=AUTH)
    assert r.status_code == 200
    keys = r.json()
    # Active keys first
    assert keys[0]["is_active"] is True
    assert keys[1]["is_active"] is True
    assert keys[2]["is_active"] is False
    # Among active, newest first
    assert keys[0]["id"] == "t3"


@pytest.mark.asyncio
@respx.mock
async def test_create_key_forces_user_email_prefix(app):
    """Key alias must always start with '{user_email}-' prefix."""
    mock_ensure_user()
    generate_route = respx.post(f"{LITELLM}/key/generate").mock(
        return_value=Response(200, json={
            "key": "sk-test1234567890abcdef1234567890abcdef1234567890ab",
            "token_id": "tok_prefix",
            "key_alias": "alice@example.com-My Key",
            "user_id": "alice@example.com",
            "created_at": "2026-03-09T00:00:00Z",
        })
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/keys", json={"name": "My Key"}, headers=AUTH)

    assert r.status_code == 201
    import json
    sent = json.loads(generate_route.calls[0].request.content)
    assert sent["key_alias"] == "alice@example.com-My Key"


@pytest.mark.asyncio
@respx.mock
async def test_create_key_no_double_prefix(app):
    """If user already includes the user_email prefix, don't double it."""
    mock_ensure_user()
    generate_route = respx.post(f"{LITELLM}/key/generate").mock(
        return_value=Response(200, json={
            "key": "sk-test1234567890abcdef1234567890abcdef1234567890ab",
            "token_id": "tok_prefix2",
            "key_alias": "alice@example.com-My Key",
            "user_id": "alice@example.com",
            "created_at": "2026-03-09T00:00:00Z",
        })
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/keys", json={"name": "alice@example.com-My Key"}, headers=AUTH)

    assert r.status_code == 201
    import json
    sent = json.loads(generate_route.calls[0].request.content)
    assert sent["key_alias"] == "alice@example.com-My Key"


@pytest.fixture
def strip_domain_app():
    from tests.conftest import create_test_app
    return create_test_app(strip_user_domain=True)


@pytest.mark.asyncio
@respx.mock
async def test_create_key_no_double_prefix_strip_domain(strip_domain_app):
    """With STRIP_USER_DOMAIN=True, a name starting with the full email prefix
    should not be double-prefixed. E.g. 'alice@corp.com-MyKey' with
    user_email='alice' should become 'alice-MyKey', not 'alice-alice@corp.com-MyKey'.
    """
    respx.get(f"{LITELLM}/user/info").mock(
        return_value=Response(404, json={"detail": "User not found"})
    )
    respx.post(f"{LITELLM}/user/new").mock(
        return_value=Response(200, json={"user_id": "alice"})
    )
    generate_route = respx.post(f"{LITELLM}/key/generate").mock(
        return_value=Response(200, json={
            "key": "sk-test1234567890abcdef1234567890abcdef1234567890ab",
            "token_id": "tok_strip",
            "key_alias": "alice-My Key",
            "user_id": "alice",
            "created_at": "2026-03-09T00:00:00Z",
        })
    )

    async with AsyncClient(transport=ASGITransport(app=strip_domain_app), base_url="http://test") as c:
        r = await c.post(
            "/api/keys",
            json={"name": "alice@corp.com-My Key"},
            headers={"X-User-Email": "alice@corp.com"},
        )

    assert r.status_code == 201
    import json
    sent = json.loads(generate_route.calls[0].request.content)
    assert sent["key_alias"] == "alice-My Key"


@pytest.mark.asyncio
@respx.mock
async def test_update_key_enforces_prefix(app):
    """PATCH endpoint must also enforce the '{user_email}-' prefix on key_alias."""
    respx.get(f"{LITELLM}/key/info").mock(
        return_value=Response(200, json={
            "token_id": "tok_abc123",
            "token": "sk-abc123fullkey",
            "key_alias": "alice@example.com-Old Name",
            "user_id": "alice@example.com",
            "blocked": False,
        })
    )
    update_route = respx.post(f"{LITELLM}/key/update").mock(
        return_value=Response(200, json={
            "token_id": "tok_abc123",
            "token": "sk-abc123fullkey",
            "key_alias": "alice@example.com-New Name",
            "user_id": "alice@example.com",
            "blocked": False,
            "created_at": "2026-03-09T00:00:00Z",
            "spend": 0,
        })
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.patch(
            "/api/keys/tok_abc123",
            json={"key_alias": "New Name"},
            headers=AUTH,
        )

    assert r.status_code == 200
    import json
    sent = json.loads(update_route.calls[0].request.content)
    assert sent["key_alias"] == "alice@example.com-New Name"


@pytest.mark.asyncio
async def test_strip_user_domain():
    from app.core.config import Settings
    from app.core.middleware import AuthMiddleware
    from fastapi import FastAPI, Request

    s = Settings(DEBUG_MODE=False, STRIP_USER_DOMAIN=True)
    app = FastAPI()
    app.add_middleware(AuthMiddleware, settings=s)

    @app.get("/api/me")
    async def me(request: Request):
        return {"email": request.state.user_email}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me", headers={"X-User-Email": "alice@corp.com"})
    assert r.status_code == 200
    assert r.json()["email"] == "alice"
