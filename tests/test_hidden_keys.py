"""Hidden keys ("hidden": true in their metadata) and HIDE_KEY_NAME_PREFIX."""
import json
from datetime import UTC, datetime

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import Settings

AUTH = {"X-User-Email": "alice@example.com"}
LITELLM = "http://mock-litellm:4000"

MINE = {
    "token": "tok-mine",
    "key_alias": "alice@example.com-laptop",
    "user_id": "alice@example.com",
    "blocked": False,
    "metadata": {"project": "x"},
    "created_at": "2026-10-02T00:00:00Z",
}
SESSION = {
    "token": "tok-session",
    "key_alias": "gw-alice-general-1760000000",
    "user_id": "alice@example.com",
    "blocked": False,
    "metadata": {"managed_by": "llm-gateway", "kind": "session", "hidden": True},
    "created_at": "2026-10-03T00:00:00Z",
}


@pytest.fixture
def app():
    from tests.conftest import create_test_app

    return create_test_app()


def _list(*keys):
    return respx.get(f"{LITELLM}/key/list").mock(
        return_value=Response(200, json={"keys": list(keys), "total_pages": 1})
    )


@pytest.mark.asyncio
@respx.mock
async def test_hidden_keys_arent_listed(app):
    _list(MINE, SESSION)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/keys", headers=AUTH)
    assert [k["id"] for k in r.json()] == ["tok-mine"]


@pytest.mark.asyncio
@respx.mock
async def test_only_the_json_boolean_hides_a_key(app):
    """Users' metadata is strings, so a key a user made can't be hidden."""
    strings = {**MINE, "token": "tok-str", "metadata": {"hidden": "true"}}
    _list(strings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/keys", headers=AUTH)
    assert [k["id"] for k in r.json()] == ["tok-str"]


@pytest.mark.asyncio
@respx.mock
async def test_hidden_keys_dont_count_toward_the_active_key_limit(app, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MAX_ACTIVE_KEYS_PER_USER", 2)
    respx.get(f"{LITELLM}/user/info").mock(return_value=Response(200, json={"user_id": "alice@example.com"}))
    generate = respx.post(f"{LITELLM}/key/generate").mock(
        return_value=Response(200, json={"key": "sk-new", "token_id": "tok-new", "key_alias": "alice@example.com-new"})
    )
    sessions = [{**SESSION, "token": f"tok-s{i}"} for i in range(5)]
    _list(MINE, *sessions)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/keys", json={"name": "new"}, headers=AUTH)
    assert r.status_code == 201 and generate.called


@pytest.mark.parametrize("method, path, body", [
    ("PATCH", "/api/keys/tok-session", {"duration": "30d"}),
    ("DELETE", "/api/keys/tok-session", None),
    ("POST", "/api/keys/tok-session/block", None),
    ("POST", "/api/keys/tok-session/unblock", None),
])
@pytest.mark.asyncio
@respx.mock
async def test_hidden_keys_cant_be_changed_through_the_app(app, method, path, body):
    respx.get(f"{LITELLM}/key/info").mock(return_value=Response(200, json={"info": SESSION}))
    writes = respx.post(url__regex=rf"{LITELLM}/key/(update|block|unblock)").mock(
        return_value=Response(200, json={})
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.request(method, path, json=body, headers=AUTH)
    assert r.status_code == 404
    assert not writes.called


@pytest.mark.asyncio
@respx.mock
async def test_users_metadata_cant_hide_a_key(app):
    respx.get(f"{LITELLM}/user/info").mock(return_value=Response(200, json={"user_id": "alice@example.com"}))
    generate = respx.post(f"{LITELLM}/key/generate").mock(
        return_value=Response(200, json={"key": "sk-new", "token_id": "tok-new", "key_alias": "alice@example.com-k"})
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/keys", json={"name": "k", "metadata": {"hidden": "true"}}, headers=AUTH)
    assert r.status_code == 201
    assert "hidden" not in json.loads(generate.calls[0].request.content)["metadata"]


@pytest.mark.asyncio
@respx.mock
async def test_the_keys_table_shows_names_without_the_user_prefix(app):
    _list(MINE, {**MINE, "token": "tok-other", "key_alias": "made-in-litellm"})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/keys", headers=AUTH)
    shown = {k["id"]: (k["name"], k["display_name"]) for k in r.json()}
    assert shown == {
        "tok-mine": ("alice@example.com-laptop", "laptop"),
        "tok-other": ("made-in-litellm", "made-in-litellm"),
    }


@pytest.mark.asyncio
@respx.mock
async def test_the_prefix_can_be_shown(app, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "HIDE_KEY_NAME_PREFIX", False)
    _list(MINE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/keys", headers=AUTH)
    assert r.json()[0]["display_name"] == "alice@example.com-laptop"


@pytest.mark.asyncio
@respx.mock
async def test_a_new_key_comes_back_with_its_shown_name(app):
    respx.get(f"{LITELLM}/user/info").mock(return_value=Response(200, json={"user_id": "alice@example.com"}))
    respx.post(f"{LITELLM}/key/generate").mock(
        return_value=Response(200, json={"key": "sk-new", "token_id": "tok-new", "key_alias": "alice@example.com-laptop"})
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/keys", json={"name": "laptop"}, headers=AUTH)
    assert r.status_code == 201
    assert (r.json()["name"], r.json()["display_name"]) == ("alice@example.com-laptop", "laptop")


@pytest.mark.parametrize("name, user, shown", [
    ("alice@example.com-laptop", "alice@example.com", "laptop"),
    ("alice-laptop", "alice", "laptop"),  # STRIP_USER_DOMAIN
    ("alice@example.com-", "alice@example.com", "alice@example.com-"),  # nothing after the prefix
    ("bob@example.com-laptop", "alice@example.com", "bob@example.com-laptop"),
    ("alice@example.com-laptop", "", "alice@example.com-laptop"),
])
def test_key_display_name(name, user, shown):
    assert Settings().key_display_name(name, user) == shown


@pytest.mark.asyncio
@respx.mock
async def test_the_usage_page_counts_hidden_keys_as_other_keys_and_shortens_names(app):
    from tests.test_routes_dashboard import _metrics

    activity = {
        "results": [{
            "date": datetime.now(UTC).date().isoformat(),
            "metrics": _metrics(spend=0.30, api_requests=3),
            "breakdown": {
                "models": {},
                "api_keys": {
                    k["token"]: {"metrics": _metrics(spend=0.10 * (i + 1), api_requests=i + 1), "metadata": {}}
                    for i, k in enumerate([MINE, SESSION])
                },
                "providers": {},
            },
        }],
        "metadata": {"page": 1, "total_pages": 1, "has_more": False},
    }
    respx.get(f"{LITELLM}/user/daily/activity").mock(return_value=Response(200, json=activity))
    respx.get(f"{LITELLM}/user/info").mock(return_value=Response(200, json={"user_id": "alice@example.com"}))
    _list(MINE, SESSION)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/dashboard", headers=AUTH)
    data = r.json()
    shown = {k["alias"]: k["display_name"] for k in data["keys"]}
    assert shown == {"alice@example.com-laptop": "laptop"}
    # The hidden key isn't listed, but its usage still counts.
    assert data["other_keys"]["key_count"] == 1
    assert data["other_keys"]["spend"] == pytest.approx(0.20)
    assert data["other_keys"]["requests"] == 2
