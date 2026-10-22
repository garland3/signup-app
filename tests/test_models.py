"""Models a key may use: GET /api/models and the checks on create and update."""
import json

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.model_access import ALL_PROXY_MODELS, NO_DEFAULT_MODELS

AUTH = {"X-User-Email": "alice@example.com"}
LITELLM = "http://mock-litellm:4000"
PROXY = ["claude-sonnet", "gpt-4o", "gpt-4o-mini"]

KEY_RESPONSE = {
    "key": "sk-test1234567890abcdef1234567890abcdef1234567890ab",
    "token_id": "tok_m",
    "key_alias": "alice@example.com-k",
    "user_id": "alice@example.com",
    "team_id": "team-alpha",
    "created_at": "2026-10-22T00:00:00Z",
    "expires": None,
    "spend": 0,
    "max_budget": None,
    "models": [],
    "blocked": False,
}


def _app(teams=True):
    from tests.conftest import create_test_app
    return create_test_app(teams_enabled=teams)


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _mock_teams(models, member_of=("team-alpha",)):
    respx.get(f"{LITELLM}/team/list").mock(
        return_value=Response(200, json=[{"team_id": t} for t in member_of])
    )
    respx.get(f"{LITELLM}/team/info", params={"team_id": "team-alpha"}).mock(
        return_value=Response(200, json={
            "team_id": "team-alpha",
            "team_info": {"team_id": "team-alpha", "models": models},
        })
    )


def _mock_proxy_models():
    return respx.get(f"{LITELLM}/v1/models").mock(
        return_value=Response(200, json={"data": [{"id": m} for m in PROXY]})
    )


def _mock_create():
    respx.get(f"{LITELLM}/user/info").mock(return_value=Response(404, json={}))
    respx.post(f"{LITELLM}/user/new").mock(return_value=Response(200, json={}))
    return respx.post(f"{LITELLM}/key/generate").mock(
        return_value=Response(200, json=KEY_RESPONSE)
    )


# ---- GET /api/models -------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_models_of_a_team_with_a_list():
    _mock_teams(["gpt-4o", "claude-sonnet", NO_DEFAULT_MODELS])
    proxy = _mock_proxy_models()
    async with _client(_app()) as c:
        r = await c.get("/api/models?team_id=team-alpha", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"team_id": "team-alpha", "models": ["claude-sonnet", "gpt-4o"]}
    assert not proxy.called  # a listed team doesn't need the proxy's list


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("team_models", [[], [ALL_PROXY_MODELS]])
async def test_unrestricted_team_offers_every_proxy_model(team_models):
    _mock_teams(team_models)
    _mock_proxy_models()
    async with _client(_app()) as c:
        r = await c.get("/api/models?team_id=team-alpha", headers=AUTH)
    assert r.json()["models"] == PROXY


@pytest.mark.asyncio
@respx.mock
async def test_locked_team_offers_no_models():
    _mock_teams([NO_DEFAULT_MODELS])
    async with _client(_app()) as c:
        r = await c.get("/api/models?team_id=team-alpha", headers=AUTH)
    assert r.json()["models"] == []


@pytest.mark.asyncio
@respx.mock
async def test_models_of_a_team_the_caller_is_not_in_is_400():
    _mock_teams(["gpt-4o"], member_of=("team-beta",))
    async with _client(_app()) as c:
        r = await c.get("/api/models?team_id=team-alpha", headers=AUTH)
    assert r.status_code == 400


@pytest.mark.asyncio
@respx.mock
async def test_models_need_a_team_when_teams_are_on():
    async with _client(_app()) as c:
        r = await c.get("/api/models", headers=AUTH)
    assert r.status_code == 400


@pytest.mark.asyncio
@respx.mock
async def test_models_without_teams_are_the_proxy_models():
    _mock_proxy_models()
    async with _client(_app(teams=False)) as c:
        r = await c.get("/api/models", headers=AUTH)
    assert r.json() == {"team_id": None, "models": PROXY}


@pytest.mark.asyncio
@respx.mock
async def test_models_upstream_failure_is_502():
    _mock_teams([])
    respx.get(f"{LITELLM}/v1/models").mock(return_value=Response(500))
    async with _client(_app()) as c:
        r = await c.get("/api/models?team_id=team-alpha", headers=AUTH)
    assert r.status_code == 502


@pytest.mark.asyncio
@respx.mock
async def test_models_need_sign_in():
    async with _client(_app()) as c:
        r = await c.get("/api/models?team_id=team-alpha")
    assert r.status_code == 401


# ---- POST /api/keys --------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_create_key_with_a_subset_of_the_team_models():
    _mock_teams(["gpt-4o", "claude-sonnet"])
    gen = _mock_create()
    async with _client(_app()) as c:
        r = await c.post("/api/keys", headers=AUTH, json={
            "name": "k", "team_id": "team-alpha", "models": ["gpt-4o", "gpt-4o"],
        })
    assert r.status_code == 201
    assert json.loads(gen.calls.last.request.content)["models"] == ["gpt-4o"]


@pytest.mark.asyncio
@respx.mock
async def test_create_key_with_a_model_outside_the_team_is_400_and_creates_nothing():
    _mock_teams(["gpt-4o"])
    gen = _mock_create()
    async with _client(_app()) as c:
        r = await c.post("/api/keys", headers=AUTH, json={
            "name": "k", "team_id": "team-alpha", "models": ["gpt-4o", "llama-3-70b"],
        })
    assert r.status_code == 400
    assert "llama-3-70b" in r.json()["detail"]
    assert not gen.called


@pytest.mark.asyncio
@respx.mock
async def test_create_key_without_models_inherits_the_team():
    _mock_teams(["gpt-4o"])
    gen = _mock_create()
    async with _client(_app()) as c:
        r = await c.post("/api/keys", headers=AUTH, json={"name": "k", "team_id": "team-alpha"})
    assert r.status_code == 201
    assert "models" not in json.loads(gen.calls.last.request.content)


@pytest.mark.asyncio
@respx.mock
async def test_create_key_without_teams_checks_the_proxy_models():
    _mock_proxy_models()
    gen = _mock_create()
    async with _client(_app(teams=False)) as c:
        bad = await c.post("/api/keys", headers=AUTH, json={"name": "k", "models": ["nope"]})
        good = await c.post("/api/keys", headers=AUTH, json={"name": "k2", "models": ["gpt-4o"]})
    assert bad.status_code == 400
    assert good.status_code == 201
    assert json.loads(gen.calls.last.request.content)["models"] == ["gpt-4o"]


@pytest.mark.asyncio
@respx.mock
async def test_many_keys_in_one_project_without_a_limit():
    # MAX_ACTIVE_KEYS_PER_USER blank (the default): no count check at all.
    _mock_teams(["gpt-4o", "claude-sonnet"])
    gen = _mock_create()
    key_list = respx.get(f"{LITELLM}/key/list")
    async with _client(_app()) as c:
        for i, model in enumerate(["gpt-4o", "claude-sonnet", "gpt-4o"]):
            r = await c.post("/api/keys", headers=AUTH, json={
                "name": f"k{i}", "team_id": "team-alpha", "models": [model],
            })
            assert r.status_code == 201
    assert gen.call_count == 3
    assert not key_list.called


@pytest.mark.asyncio
@respx.mock
async def test_team_wildcard_allows_matching_models_only():
    _mock_teams(["openai/*"])
    gen = _mock_create()
    async with _client(_app()) as c:
        ok = await c.post("/api/keys", headers=AUTH, json={
            "name": "k", "team_id": "team-alpha", "models": ["openai/gpt-4o"],
        })
        bad = await c.post("/api/keys", headers=AUTH, json={
            "name": "k2", "team_id": "team-alpha", "models": ["anthropic/claude"],
        })
    assert ok.status_code == 201
    assert json.loads(gen.calls.last.request.content)["models"] == ["openai/gpt-4o"]
    assert bad.status_code == 400
    assert gen.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_access_group_name_not_on_the_proxy_list_is_400():
    # Deliberately strict: a key may only name models the proxy (or project)
    # lists, so an access-group name is refused even if LiteLLM would expand it.
    _mock_proxy_models()
    gen = _mock_create()
    async with _client(_app(teams=False)) as c:
        r = await c.post("/api/keys", headers=AUTH, json={"name": "k", "models": ["beta-models"]})
    assert r.status_code == 400
    assert not gen.called


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status, expected", [(404, 400), (500, 502)])
async def test_create_key_when_the_team_cannot_be_read_creates_nothing(status, expected):
    respx.get(f"{LITELLM}/team/list").mock(
        return_value=Response(200, json=[{"team_id": "team-alpha"}])
    )
    respx.get(f"{LITELLM}/team/info").mock(return_value=Response(status, json={}))
    gen = _mock_create()
    async with _client(_app()) as c:
        r = await c.post("/api/keys", headers=AUTH, json={
            "name": "k", "team_id": "team-alpha", "models": ["gpt-4o"],
        })
    assert r.status_code == expected
    assert not gen.called


@pytest.mark.asyncio
@respx.mock
async def test_create_key_when_the_proxy_models_cannot_be_read_creates_nothing():
    _mock_teams([])  # unrestricted team: needs the proxy's list
    respx.get(f"{LITELLM}/v1/models").mock(return_value=Response(500))
    gen = _mock_create()
    async with _client(_app()) as c:
        r = await c.post("/api/keys", headers=AUTH, json={
            "name": "k", "team_id": "team-alpha", "models": ["gpt-4o"],
        })
    assert r.status_code == 502
    assert not gen.called


# ---- PATCH /api/keys/{token} ----------------------------------------------

def _mock_owned_key(team_id="team-alpha"):
    respx.get(f"{LITELLM}/key/info").mock(return_value=Response(200, json={
        "token_id": "tok_m", "key_alias": "alice@example.com-k",
        "user_id": "alice@example.com", "team_id": team_id, "blocked": False,
    }))
    return respx.post(f"{LITELLM}/key/update").mock(return_value=Response(200, json={
        **KEY_RESPONSE, "models": ["gpt-4o"],
    }))


@pytest.mark.asyncio
@respx.mock
async def test_update_key_models_within_the_team():
    _mock_teams(["gpt-4o", "claude-sonnet"])
    upd = _mock_owned_key()
    async with _client(_app()) as c:
        r = await c.patch("/api/keys/tok_m", headers=AUTH, json={"models": ["gpt-4o"]})
    assert r.status_code == 200
    assert json.loads(upd.calls.last.request.content)["models"] == ["gpt-4o"]


@pytest.mark.asyncio
@respx.mock
async def test_update_key_cannot_widen_beyond_the_team():
    _mock_teams(["gpt-4o"])
    upd = _mock_owned_key()
    async with _client(_app()) as c:
        r = await c.patch("/api/keys/tok_m", headers=AUTH, json={"models": ["claude-sonnet"]})
    assert r.status_code == 400
    assert not upd.called


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status, expected", [(404, 400), (500, 502)])
async def test_update_key_when_the_team_cannot_be_read_changes_nothing(status, expected):
    respx.get(f"{LITELLM}/team/info").mock(return_value=Response(status, json={}))
    upd = _mock_owned_key()
    async with _client(_app()) as c:
        r = await c.patch("/api/keys/tok_m", headers=AUTH, json={"models": ["gpt-4o"]})
    assert r.status_code == expected
    assert not upd.called
