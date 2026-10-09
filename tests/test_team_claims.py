"""TEAM_SOURCE=claims: teams from the identity provider's groups."""
import json
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from app.core import oidc, team_claims
from app.core.middleware import AuthMiddleware
from app.core.rate_limit import limiter
from app.core.sessions import InMemorySessionMiddleware
from app.routes.auth import router as auth_router
from app.routes.keys import router as keys_router
from app.routes.teams import router as teams_router
from app.routes.users import router as users_router
from tests.test_oidc import ISSUER, LITELLM, _mock_provider, _settings, _token

TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
ORIGIN = {"Origin": "http://test"}
TEAMS = [
    {"team_id": "t-alpha", "team_alias": "project-alpha"},
    {"team_id": "project-general", "team_alias": None},  # matched by team_id
    {"team_id": "t-other", "team_alias": "other-team"},  # no prefix: never a team here
]


def _claims_settings(**overrides):
    return _settings(FEATURE_TEAMS_ENABLED=True, TEAM_SOURCE="claims", **overrides)


def _app(settings) -> FastAPI:
    import app.core.config as config_mod

    config_mod.settings = settings
    limiter.reset()
    app = FastAPI()
    app.add_middleware(AuthMiddleware, settings=settings)
    app.add_middleware(
        InMemorySessionMiddleware,
        cookie_name=settings.SESSION_COOKIE_NAME,
        max_age=settings.SESSION_MAX_AGE,
        idle_timeout=settings.SESSION_IDLE_TIMEOUT,
        https_only=False,
    )
    for r in (auth_router, users_router, keys_router, teams_router):
        app.include_router(r)
    return app


@pytest.fixture(autouse=True)
def _fresh_caches():
    oidc.reset_caches()
    team_claims.reset_cache()
    yield
    oidc.reset_caches()
    team_claims.reset_cache()


class Provider:
    """The token endpoint: the sign-in's tokens, then each refresh's."""

    def __init__(self, groups, *, refresh_token="rt-1"):
        self.groups = groups  # groups in the next ID token
        self.refresh_token = refresh_token
        self.refresh_status = 200
        self.sub = "0b9f-sub"
        self.refreshes = 0

    def __call__(self, request):
        form = parse_qs(request.content.decode())
        grant = form["grant_type"][0]
        if grant == "refresh_token":
            self.refreshes += 1
            if self.refresh_status != 200:
                return Response(self.refresh_status, json={"error": "invalid_grant"})
            body = {"access_token": "at-2", "id_token": _token(groups=self.groups, sub=self.sub)}
            body["refresh_token"] = f"rt-{self.refreshes + 1}"
            return Response(200, json=body)
        body = {"access_token": "at-1", "id_token": _token(groups=self.groups, nonce=self.nonce)}
        if self.refresh_token:
            body["refresh_token"] = self.refresh_token
        return Response(200, json=body)


async def _sign_in(c: AsyncClient, provider: Provider):
    login = await c.get("/api/auth/login", follow_redirects=False)
    q = parse_qs(urlparse(login.headers["location"]).query)
    provider.nonce = q["nonce"][0]
    cb = await c.get(f"/api/auth/callback?code=abc&state={q['state'][0]}", follow_redirects=False)
    assert cb.status_code == 302


def _litellm(*, member_add_status=200, member_add_body=None):
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=TEAMS))
    respx.get(f"{LITELLM}/user/info").mock(return_value=Response(200, json={"user_id": "0b9f-sub"}))
    member_add = respx.post(f"{LITELLM}/team/member_add").mock(
        return_value=Response(member_add_status, json=member_add_body or {})
    )
    generate = respx.post(f"{LITELLM}/key/generate").mock(
        return_value=Response(200, json={"key": "sk-new", "token_id": "t1", "user_id": "0b9f-sub"})
    )
    return member_add, generate


GROUPS = ["project-alpha", "project-general", "project-gamma", "staff"]


@pytest.mark.asyncio
@respx.mock
async def test_me_lists_the_groups_that_name_teams():
    _mock_provider()
    _litellm()
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        me = (await c.get("/api/me")).json()
    assert me["team_source"] == "claims"
    assert me["teams"] == [
        {"team_id": "t-alpha", "team_alias": "project-alpha"},
        {"team_id": "project-general", "team_alias": "project-general"},
    ]  # not project-gamma (no team), staff or other-team (no prefix)
    assert me["teams_unavailable"] is False


@pytest.mark.asyncio
@respx.mock
async def test_no_self_join():
    _mock_provider()
    _litellm()
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        assert (await c.get("/api/teams/available")).status_code == 404
        join = await c.post("/api/teams/join", json={"team_id": "t-other"}, headers=ORIGIN)
        assert join.status_code == 404


@pytest.mark.asyncio
@respx.mock
async def test_key_in_a_team_refreshes_groups_and_adds_the_member():
    _mock_provider()
    member_add, generate = _litellm()
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
    assert r.status_code == 201
    assert provider.refreshes == 1
    member = json.loads(member_add.calls[0].request.content)
    assert member == {"team_id": "t-alpha", "member": {"user_id": "0b9f-sub", "role": "user"}}
    key = json.loads(generate.calls[0].request.content)
    assert key["team_id"] == "t-alpha" and key["user_id"] == "0b9f-sub"


@pytest.mark.asyncio
@respx.mock
async def test_removed_from_a_group_since_sign_in_no_key_in_it():
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        provider.groups = ["project-general"]  # taken out of project-alpha
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
        assert r.status_code == 400
        assert not generate.called
        teams = (await c.get("/api/me")).json()["teams"]
    assert [t["team_alias"] for t in teams] == ["project-general"]


@pytest.mark.asyncio
@respx.mock
async def test_refresh_refused_ends_the_session():
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        provider.refresh_status = 400  # session ended, or the person was disabled
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
        assert r.status_code == 401
        assert (await c.get("/api/me")).status_code == 401
    assert not generate.called


@pytest.mark.asyncio
@respx.mock
async def test_refresh_for_another_person_ends_the_session():
    _mock_provider()
    _litellm()
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        provider.sub = "someone-else"
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
    assert r.status_code == 401


@pytest.mark.asyncio
@respx.mock
async def test_without_a_refresh_token_the_sign_in_groups_apply():
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS, refresh_token=None)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        r = await c.post("/api/keys", json={"name": "k", "team_id": "project-general"}, headers=ORIGIN)
    assert r.status_code == 201
    assert provider.refreshes == 0
    assert json.loads(generate.calls[0].request.content)["team_id"] == "project-general"


@pytest.mark.asyncio
@respx.mock
async def test_already_a_member_is_fine():
    _mock_provider()
    _, generate = _litellm(member_add_status=400, member_add_body={"error": "User already in team"})
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
    assert r.status_code == 201
    assert generate.called


@pytest.mark.asyncio
@respx.mock
async def test_member_add_failure_blocks_the_key():
    _mock_provider()
    _, generate = _litellm(member_add_status=500)
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
    assert r.status_code == 502
    assert not generate.called


@pytest.mark.asyncio
@respx.mock
async def test_a_key_needs_a_team():
    _mock_provider()
    _litellm()
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        r = await c.post("/api/keys", json={"name": "k"}, headers=ORIGIN)
    assert r.status_code == 400
    assert "sign-in" in r.json()["detail"]


@pytest.mark.asyncio
@respx.mock
async def test_entra_app_roles_and_a_single_string_claim():
    _mock_provider()
    _litellm()
    provider = Provider(None)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    settings = _claims_settings(GROUPS_FIELD="roles")

    def tokens(request):
        form = parse_qs(request.content.decode())
        assert form["grant_type"][0] == "authorization_code"
        return Response(200, json={"access_token": "at", "id_token": _token(roles="project-alpha", nonce=provider.nonce)})

    respx.post(TOKEN_URL).mock(side_effect=tokens)
    async with AsyncClient(transport=ASGITransport(app=_app(settings)), base_url="http://test") as c:
        await _sign_in(c, provider)
        teams = (await c.get("/api/me")).json()["teams"]
    assert [t["team_id"] for t in teams] == ["t-alpha"]


@pytest.mark.asyncio
@respx.mock
async def test_a_team_created_after_the_cache_was_loaded_is_found():
    _mock_provider()
    listing = respx.get(f"{LITELLM}/team/list").mock(
        side_effect=[Response(200, json=TEAMS[:1]), Response(200, json=TEAMS)]
    )
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        first = (await c.get("/api/me")).json()["teams"]
    # project-gamma has no team in either listing, so the cache was reloaded once.
    assert [t["team_alias"] for t in first] == ["project-alpha", "project-general"]
    assert listing.call_count == 2


def test_startup_refuses_claims_without_oauth_or_teams():
    from app.main import _enforce_startup_safety

    with pytest.raises(RuntimeError, match="TEAM_SOURCE=claims"):
        _enforce_startup_safety(_settings(FEATURE_TEAMS_ENABLED=False, TEAM_SOURCE="claims"))
