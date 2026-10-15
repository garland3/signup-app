"""Every key, not LiteLLM's first page: /key/list is paged (10 keys by default)."""
from datetime import UTC, datetime

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core import litellm_client
from app.core.config import Settings
from app.core.litellm_client import LiteLLMClient

AUTH = {"X-User-Email": "alice@example.com"}
LITELLM = "http://mock-litellm:4000"


def _keys(n, *, blocked=False):
    return [
        {
            "token": f"tok-{i:03d}",
            "key_alias": f"alice@example.com-k{i:03d}",
            "user_id": "alice@example.com",
            "blocked": blocked,
            "created_at": f"2026-10-{1 + i // 24:02d}T{i % 24:02d}:00:00Z",
        }
        for i in range(n)
    ]


def _paged(keys, *, total_pages=None):
    """A /key/list that pages like LiteLLM's (10 keys unless asked for more),
    recording the params it was given."""
    calls = []

    def endpoint(request):
        params = dict(request.url.params)
        calls.append(params)
        page = int(params.get("page", 1))
        n = int(params.get("size", 10))
        pages = total_pages if total_pages is not None else max(1, -(-len(keys) // n))
        return Response(200, json={
            "keys": keys[(page - 1) * n:page * n],
            "total_count": len(keys),
            "current_page": page,
            "total_pages": pages,
        })

    route = respx.get(f"{LITELLM}/key/list").mock(side_effect=endpoint)
    return route, calls


def _client():
    return LiteLLMClient(Settings(LITELLM_BASE_URL=LITELLM, LITELLM_ADMIN_KEY="sk-test"))


@pytest.mark.asyncio
@respx.mock
async def test_list_keys_reads_every_page():
    keys = _keys(250)
    _, calls = _paged(keys)
    result = await _client().list_keys("alice@example.com")
    assert [k["token"] for k in result["keys"]] == [k["token"] for k in keys]
    assert [c["page"] for c in calls] == ["1", "2", "3"]
    assert {(c["size"], c["sort_by"], c["sort_order"]) for c in calls} == {("100", "created_at", "desc")}


@pytest.mark.asyncio
@respx.mock
async def test_a_key_that_shifts_pages_is_listed_once():
    keys = _keys(150)
    pages = [keys[:100], keys[99:150]]  # a key created meanwhile pushed tok-099 onto page 2

    def endpoint(request):
        page = int(request.url.params["page"])
        return Response(200, json={"keys": pages[page - 1], "total_pages": 2})

    respx.get(f"{LITELLM}/key/list").mock(side_effect=endpoint)
    result = await _client().list_keys("alice@example.com")
    tokens = [k["token"] for k in result["keys"]]
    assert len(tokens) == len(set(tokens)) == 150


@pytest.mark.asyncio
@respx.mock
async def test_a_proxy_that_doesnt_page_is_read_once():
    route = respx.get(f"{LITELLM}/key/list").mock(return_value=Response(200, json=_keys(3)))
    result = await _client().list_keys("alice@example.com")
    assert len(result["keys"]) == 3 and route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_paging_stops_at_the_cap(monkeypatch, caplog):
    monkeypatch.setattr(litellm_client, "KEY_LIST_MAX_PAGES", 2)
    calls = []

    def endpoint(request):  # a proxy that never runs out of pages
        page = int(request.url.params["page"])
        calls.append(page)
        return Response(200, json={"keys": [{"token": f"tok-{page}"}], "total_pages": 1000})

    respx.get(f"{LITELLM}/key/list").mock(side_effect=endpoint)
    with caplog.at_level("WARNING", logger="app.core.litellm_client"):
        await _client().list_keys("alice@example.com")
    assert len(calls) == 2
    assert "stopped at 2 pages" in caplog.text


@pytest.fixture
def app():
    from tests.conftest import create_test_app

    return create_test_app()


@pytest.mark.asyncio
@respx.mock
async def test_the_keys_table_lists_every_key(app):
    _paged(_keys(250))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/keys", headers=AUTH)
    assert r.status_code == 200 and len(r.json()) == 250


@pytest.mark.asyncio
@respx.mock
async def test_the_active_key_limit_counts_every_key(app, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MAX_ACTIVE_KEYS_PER_USER", 120)
    respx.get(f"{LITELLM}/user/info").mock(return_value=Response(200, json={"user_id": "alice@example.com"}))
    generate = respx.post(f"{LITELLM}/key/generate").mock(return_value=Response(200, json={"key": "sk-new"}))
    _paged(_keys(150))  # 150 active keys over two pages: past the limit
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/keys", json={"name": "X"}, headers=AUTH)
    assert r.status_code == 400 and "limit" in r.json()["detail"].lower()
    assert not generate.called


@pytest.mark.asyncio
async def test_the_mock_proxy_pages_like_litellm(monkeypatch):
    from mocks import litellm_mock

    monkeypatch.setattr(litellm_mock, "keys_db", {
        k["token"]: {**k, "token": k["token"] + "-full"} for k in _keys(25)
    })
    headers = {"authorization": f"Bearer {litellm_mock.ADMIN_KEY}"}
    async with AsyncClient(transport=ASGITransport(app=litellm_mock.app), base_url="http://mock") as c:
        first = (await c.get("/key/list", params={"user_id": "alice@example.com"}, headers=headers)).json()
        last = (await c.get("/key/list", params={"user_id": "alice@example.com", "page": 3}, headers=headers)).json()
        too_big = await c.get("/key/list", params={"size": 101}, headers=headers)
    assert (len(first["keys"]), first["total_pages"], first["total_count"]) == (10, 3, 25)
    assert len(last["keys"]) == 5
    assert first["keys"][0]["created_at"] > first["keys"][-1]["created_at"]  # newest first
    assert too_big.status_code == 422


@pytest.mark.asyncio
@respx.mock
async def test_the_usage_page_names_keys_from_every_page(app):
    from tests.test_routes_dashboard import _metrics

    keys = _keys(250)
    last = keys[-1]  # on the third page
    activity = {
        "results": [{
            "date": datetime.now(UTC).date().isoformat(),
            "metrics": _metrics(spend=0.10, api_requests=1),
            "breakdown": {
                "models": {},
                "api_keys": {last["token"]: {"metrics": _metrics(spend=0.10, api_requests=1), "metadata": {}}},
                "providers": {},
            },
        }],
        "metadata": {"page": 1, "total_pages": 1, "has_more": False},
    }
    respx.get(f"{LITELLM}/user/daily/activity").mock(return_value=Response(200, json=activity))
    respx.get(f"{LITELLM}/user/info").mock(return_value=Response(200, json={"user_id": "alice@example.com"}))
    _paged(keys)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/dashboard", headers=AUTH)
    assert r.status_code == 200
    assert [k["alias"] for k in r.json()["keys"]] == [last["key_alias"]]


@pytest.mark.asyncio
@respx.mock
async def test_a_failed_later_page_fails_the_list(app, monkeypatch):
    """A page that fails isn't skipped: the list fails, and key creation, which
    counts active keys, is refused rather than undercounting."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MAX_ACTIVE_KEYS_PER_USER", 500)
    keys = _keys(150)

    def endpoint(request):
        if request.url.params["page"] == "2":
            return Response(500, json={"error": "boom"})
        return Response(200, json={"keys": keys[:100], "total_pages": 2})

    respx.get(f"{LITELLM}/key/list").mock(side_effect=endpoint)
    respx.get(f"{LITELLM}/user/info").mock(return_value=Response(200, json={"user_id": "alice@example.com"}))
    generate = respx.post(f"{LITELLM}/key/generate").mock(return_value=Response(200, json={"key": "sk-new"}))
    with pytest.raises(httpx.HTTPStatusError):
        await _client().list_keys("alice@example.com")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        listed = await c.get("/api/keys", headers=AUTH)
        created = await c.post("/api/keys", json={"name": "X"}, headers=AUTH)
    assert listed.status_code == 502 and created.status_code == 502
    assert not generate.called
