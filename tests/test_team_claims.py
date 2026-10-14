"""TEAM_SOURCE=claims: teams from the identity provider's groups."""
import asyncio
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


@pytest.fixture
def audited(monkeypatch):
    """The audit events the claims code emits: [(event, fields)]."""
    from app.routes import keys as keys_routes

    events = []

    def record(event, **fields):
        events.append((event, fields))

    monkeypatch.setattr(team_claims, "audit", record)
    monkeypatch.setattr(keys_routes, "audit", record)
    return events


class Provider:
    """The token endpoint: the sign-in's tokens, then each refresh's."""

    def __init__(self, groups, *, refresh_token="rt-1"):
        self.groups = groups  # groups in the next ID token (None: no groups claim)
        self.extra = {}  # other claims in the next ID token
        self.refresh_token = refresh_token
        self.refresh_status = 200
        self.refresh_error = "invalid_grant"  # the error code with a refused refresh
        self.refresh_id_token = True  # False: a refresh response without an ID token
        self.used = set()  # refresh tokens already used: reusing one is refused (rotation)
        self.sub = "0b9f-sub"
        self.refreshes = 0

    def __call__(self, request):
        form = parse_qs(request.content.decode())
        grant = form["grant_type"][0]
        if grant == "refresh_token":
            token = form["refresh_token"][0]
            if token in self.used:  # Keycloak "Revoke Refresh Token": reuse is refused
                return Response(400, json={"error": "invalid_grant"})
            self.used.add(token)
            self.refreshes += 1
            if self.refresh_status != 200:
                return Response(self.refresh_status, json={"error": self.refresh_error})
            body = {"access_token": "at-2"}
            if self.refresh_id_token:
                body["id_token"] = _token(self._claims(), sub=self.sub)
            body["refresh_token"] = f"rt-{self.refreshes + 1}"
            return Response(200, json=body)
        body = {"access_token": "at-1", "id_token": _token(self._claims(), nonce=self.nonce)}
        if self.refresh_token:
            body["refresh_token"] = self.refresh_token
        return Response(200, json=body)

    def _claims(self):
        claims = dict(self.extra)
        if self.groups is not None:
            claims["groups"] = self.groups
        return claims


async def _sign_in(c: AsyncClient, provider: Provider):
    login = await c.get("/api/auth/login", follow_redirects=False)
    q = parse_qs(urlparse(login.headers["location"]).query)
    provider.nonce = q["nonce"][0]
    cb = await c.get(f"/api/auth/callback?code=abc&state={q['state'][0]}", follow_redirects=False)
    assert cb.status_code == 302


def _litellm(*, members=(), member_add_status=200, member_after_failed_add=False):
    """LiteLLM: every team (/team/list), the person's teams (?user_id=, from
    ``members``), member_add (which adds to ``members`` unless it fails) and
    key generation."""
    members = set(members)

    def team_list(request):
        if request.url.params.get("user_id"):
            return Response(200, json=[t for t in TEAMS if t["team_id"] in members])
        return Response(200, json=TEAMS)

    def add(request):
        team = json.loads(request.content)["team_id"]
        if member_add_status != 200:
            if member_after_failed_add:  # someone else's request added them meanwhile
                members.add(team)
            return Response(member_add_status, json={"error": "failed"})
        members.add(team)
        return Response(200, json={})

    respx.get(f"{LITELLM}/team/list").mock(side_effect=team_list)
    respx.get(f"{LITELLM}/user/info").mock(return_value=Response(200, json={"user_id": "0b9f-sub"}))
    member_add = respx.post(f"{LITELLM}/team/member_add").mock(side_effect=add)
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
async def test_key_in_a_team_refreshes_groups_and_adds_the_member(audited):
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
    assert ("team_claims_member_add", {"user": "alice@example.com", "team_id": "t-alpha"}) in audited


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
async def test_refresh_for_another_person_ends_the_session(audited):
    _mock_provider()
    _litellm()
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        provider.sub = "someone-else"
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
    assert r.status_code == 401
    assert [e for e, _ in audited if e == "oauth_refresh_other_user"] == ["oauth_refresh_other_user"]


@pytest.mark.asyncio
@respx.mock
async def test_without_a_refresh_token_signing_in_again_lets_a_key_be_made(monkeypatch):
    """No sign-in loop: a fresh sign-in's groups are accepted; older ones need a
    new sign-in (401), after which the key can be made."""
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS, refresh_token=None)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    clock = [team_claims._now()]
    monkeypatch.setattr(team_claims, "_now", lambda: clock[0])
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        first = await c.post("/api/keys", json={"name": "k1", "team_id": "project-general"}, headers=ORIGIN)
        clock[0] += team_claims.SIGN_IN_GRACE_SECONDS + 60
        stale = await c.post("/api/keys", json={"name": "k2", "team_id": "project-general"}, headers=ORIGIN)
        await _sign_in(c, provider)
        again = await c.post("/api/keys", json={"name": "k2", "team_id": "project-general"}, headers=ORIGIN)
    assert (first.status_code, stale.status_code, again.status_code) == (201, 401, 201)
    assert provider.refreshes == 0
    assert generate.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_a_sign_in_without_a_refresh_token_drops_the_previous_one():
    _mock_provider()
    _litellm()
    provider = Provider(GROUPS)  # the first sign-in gets a refresh token
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        provider.refresh_token = None  # the next sign-in doesn't
        await _sign_in(c, provider)
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
    assert r.status_code == 201
    assert provider.refreshes == 0  # the first sign-in's token wasn't used


@pytest.mark.asyncio
@respx.mock
async def test_without_a_refresh_token_recent_groups_within_the_max_age():
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS, refresh_token=None)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    settings = _claims_settings(TEAM_CLAIMS_MAX_AGE_SECONDS=300)
    async with AsyncClient(transport=ASGITransport(app=_app(settings)), base_url="http://test") as c:
        await _sign_in(c, provider)
        r = await c.post("/api/keys", json={"name": "k", "team_id": "project-general"}, headers=ORIGIN)
    assert r.status_code == 201
    assert json.loads(generate.calls[0].request.content)["team_id"] == "project-general"


@pytest.mark.asyncio
@respx.mock
async def test_without_a_refresh_token_groups_older_than_the_max_age(monkeypatch):
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS, refresh_token=None)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    settings = _claims_settings(TEAM_CLAIMS_MAX_AGE_SECONDS=300)
    async with AsyncClient(transport=ASGITransport(app=_app(settings)), base_url="http://test") as c:
        await _sign_in(c, provider)
        later = team_claims._now() + 400
        monkeypatch.setattr(team_claims, "_now", lambda: later)
        r = await c.post("/api/keys", json={"name": "k", "team_id": "project-general"}, headers=ORIGIN)
    assert r.status_code == 401
    assert not generate.called


@pytest.mark.asyncio
@respx.mock
async def test_a_refreshed_token_without_groups_has_no_teams():
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        provider.groups = None  # the refreshed ID token has no groups claim
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
    assert r.status_code == 400  # not a team of theirs any more
    assert not generate.called


OVERAGE = {"_claim_names": {"groups": "src1"}, "_claim_sources": {"src1": {"endpoint": "https://graph.example/x"}}}


@pytest.mark.asyncio
@respx.mock
async def test_groups_overage_at_refresh_is_refused():
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        provider.groups, provider.extra = None, OVERAGE  # too many groups to list
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
        assert r.status_code == 403
        assert "too many" in r.json()["detail"]
        me = (await c.get("/api/me")).json()
    assert not generate.called
    assert me["teams"] == [] and me["teams_unavailable"] is True  # unknown, not none


@pytest.mark.asyncio
@respx.mock
async def test_groups_overage_at_sign_in_shows_teams_unknown():
    _mock_provider()
    _litellm()
    provider = Provider(None)
    provider.extra = {"hasgroups": True}
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        me = (await c.get("/api/me")).json()
    assert me["teams"] == [] and me["teams_unavailable"] is True


@pytest.mark.asyncio
@respx.mock
async def test_already_a_member_is_not_added_again(audited):
    _mock_provider()
    member_add, generate = _litellm(members={"t-alpha"})
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
    assert r.status_code == 201
    assert not member_add.called  # membership looked up, not inferred from an error
    assert generate.called
    assert "team_claims_member_add" not in [e for e, _ in audited]


@pytest.mark.asyncio
@respx.mock
async def test_a_failed_add_is_fine_if_they_are_a_member_after_all():
    _mock_provider()
    _, generate = _litellm(member_add_status=400, member_after_failed_add=True)
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
    assert r.status_code == 201  # a concurrent request added them
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


def _key(team_id="t-alpha"):
    """A key of the signed-in person's in team_id, and the routes that change it."""
    info = {"info": {"token": "hashed-1", "user_id": "0b9f-sub", "team_id": team_id, "key_alias": "alice-k"}}
    respx.get(f"{LITELLM}/key/info").mock(return_value=Response(200, json=info))
    return {
        name: respx.post(f"{LITELLM}/key/{name}").mock(return_value=Response(200, json=info["info"]))
        for name in ("update", "block", "unblock", "delete")
    }


@pytest.mark.asyncio
@respx.mock
async def test_left_the_group_no_changing_or_unblocking_its_keys():
    _mock_provider()
    _litellm()
    routes = _key("t-alpha")
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        provider.groups = ["project-general"]  # taken out of project-alpha
        patch = await c.patch("/api/keys/hashed-1", json={"max_budget": 100}, headers=ORIGIN)
        unblock = await c.post("/api/keys/hashed-1/unblock", headers=ORIGIN)
        assert patch.status_code == 403 and unblock.status_code == 403
        assert not routes["update"].called and not routes["unblock"].called
        block = await c.post("/api/keys/hashed-1/block", headers=ORIGIN)
        delete = await c.delete("/api/keys/hashed-1", headers=ORIGIN)
    assert block.status_code == 200 and delete.status_code in (200, 204)  # these only reduce access


@pytest.mark.asyncio
@respx.mock
async def test_still_in_the_group_changes_its_keys():
    _mock_provider()
    _litellm()
    routes = _key("t-alpha")
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        patch = await c.patch("/api/keys/hashed-1", json={"max_budget": 100}, headers=ORIGIN)
        unblock = await c.post("/api/keys/hashed-1/unblock", headers=ORIGIN)
    assert patch.status_code == 200 and unblock.status_code == 200
    assert routes["update"].called and routes["unblock"].called


@pytest.mark.asyncio
@respx.mock
async def test_a_refresh_error_other_than_invalid_grant_keeps_the_session():
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        provider.refresh_status, provider.refresh_error = 401, "invalid_client"  # e.g. a rotated client secret
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
        me = await c.get("/api/me")
    assert r.status_code == 502  # the app's problem, not the person's
    assert me.status_code == 200  # still signed in
    assert not generate.called


@pytest.mark.asyncio
@respx.mock
async def test_concurrent_requests_share_one_refresh():
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS)

    async def slow_provider(request):
        if parse_qs(request.content.decode())["grant_type"][0] == "refresh_token":
            await asyncio.sleep(0.05)  # keep the first refresh in flight
        return provider(request)

    respx.post(TOKEN_URL).mock(side_effect=slow_provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        a, b = await asyncio.gather(
            c.post("/api/keys", json={"name": "k1", "team_id": "t-alpha"}, headers=ORIGIN),
            c.post("/api/keys", json={"name": "k2", "team_id": "t-alpha"}, headers=ORIGIN),
        )
        me = await c.get("/api/me")
    assert (a.status_code, b.status_code) == (201, 201)  # neither reused the refresh token
    assert provider.refreshes == 1
    assert me.status_code == 200
    assert generate.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_a_refresh_without_an_id_token_says_so():
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        provider.refresh_id_token = False
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
    assert r.status_code == 502
    assert "no ID token" in r.json()["detail"]
    assert not generate.called


@pytest.mark.asyncio
@respx.mock
async def test_hasgroups_is_about_groups_not_app_roles():
    _mock_provider()
    _litellm()
    provider = Provider(None)
    provider.extra = {"roles": ["project-alpha"], "hasgroups": True}
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings(GROUPS_FIELD="roles"))), base_url="http://test") as c:
        await _sign_in(c, provider)
        me = (await c.get("/api/me")).json()
    assert [t["team_id"] for t in me["teams"]] == ["t-alpha"] and me["teams_unavailable"] is False


@pytest.mark.asyncio
@respx.mock
async def test_a_group_without_a_team_reloads_the_teams_at_most_every_few_seconds():
    _mock_provider()
    _litellm()
    provider = Provider(GROUPS)  # project-gamma names no team
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        for _ in range(3):
            assert (await c.get("/api/me")).status_code == 200
    all_teams = [
        call for call in respx.calls
        if call.request.url.path == "/team/list" and not call.request.url.params.get("user_id")
    ]
    assert len(all_teams) == 2  # the first load, and one forced reload


@pytest.mark.asyncio
@respx.mock
async def test_the_rotated_refresh_token_is_kept():
    _mock_provider()
    _litellm()
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        first = await c.post("/api/keys", json={"name": "k1", "team_id": "t-alpha"}, headers=ORIGIN)
        second = await c.post("/api/keys", json={"name": "k2", "team_id": "t-alpha"}, headers=ORIGIN)
    assert (first.status_code, second.status_code) == (201, 201)
    assert provider.refreshes == 2  # rt-1, then rt-2 (reusing rt-1 would have been refused)


@pytest.mark.asyncio
@pytest.mark.parametrize("error, status", [("invalid_grant", 401), ("invalid_client", 502)])
@respx.mock
async def test_concurrent_requests_share_a_refused_refresh(error, status):
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS)

    async def slow_provider(request):
        if parse_qs(request.content.decode())["grant_type"][0] == "refresh_token":
            await asyncio.sleep(0.05)
        return provider(request)

    respx.post(TOKEN_URL).mock(side_effect=slow_provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        provider.refresh_status, provider.refresh_error = 400, error
        a, b = await asyncio.gather(
            c.post("/api/keys", json={"name": "k1", "team_id": "t-alpha"}, headers=ORIGIN),
            c.post("/api/keys", json={"name": "k2", "team_id": "t-alpha"}, headers=ORIGIN),
        )
    assert (a.status_code, b.status_code) == (status, status)
    assert provider.refreshes == 1
    assert not generate.called


@pytest.mark.asyncio
async def test_a_cancelled_request_doesnt_cancel_the_shared_refresh(monkeypatch):
    calls = []
    release = asyncio.Event()

    async def fake_refresh(refresh_token, s):
        calls.append(refresh_token)
        await release.wait()
        return {"refresh_token": "rt-new"}, {"sub": "x"}

    monkeypatch.setattr(team_claims, "_refresh", fake_refresh)
    session = {"refresh_token": "rt-old"}
    s = _claims_settings()
    leader = asyncio.create_task(team_claims._refresh_shared("rt-old", s, session))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(team_claims._refresh_shared("rt-old", s, session))
    await asyncio.sleep(0)
    leader.cancel()  # its client went away
    release.set()
    tokens, _ = await waiter
    assert leader.cancelled()
    assert tokens["refresh_token"] == "rt-new" and calls == ["rt-old"]
    assert session["refresh_token"] == "rt-new"  # the rotation isn't lost


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "teams",
    [
        # one team's alias is another's ID
        [{"team_id": "project-alpha", "team_alias": "legacy"}, {"team_id": "t-real", "team_alias": "project-alpha"}],
        # two teams share an alias
        [{"team_id": "t-first", "team_alias": "project-alpha"}, {"team_id": "t-second", "team_alias": "project-alpha"}],
    ],
)
async def test_a_group_name_that_could_mean_two_teams_means_none(teams, caplog):
    _mock_provider()
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=teams))
    provider = Provider(["project-alpha"])
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        with caplog.at_level("WARNING", logger="app.core.team_claims"):
            me = (await c.get("/api/me")).json()
    assert me["teams"] == []
    assert "Group name project-alpha could mean teams" in caplog.text


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "status, body",
    [
        (200, b"<html>a proxy's page</html>"),  # a 200 that isn't JSON
        (200, b"[]"),  # JSON, but not an object
        (500, b"<html>error</html>"),  # an error that isn't JSON
        (400, b'"invalid_grant"'),  # an error body that isn't an object
    ],
)
async def test_an_unexpected_token_endpoint_answer_is_a_502(status, body):
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        respx.post(TOKEN_URL).mock(side_effect=provider)
        await _sign_in(c, provider)
        respx.post(TOKEN_URL).mock(return_value=Response(status, content=body))
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
        me = await c.get("/api/me")
    assert r.status_code == 502
    assert me.status_code == 200  # still signed in
    assert not generate.called


@pytest.mark.asyncio
@respx.mock
async def test_key_changes_that_re_read_groups_are_rate_limited():
    _mock_provider()
    _litellm()
    routes = _key("t-alpha")
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    settings = _claims_settings(RATE_LIMIT_KEY_CREATE_PER_HOUR=2)
    async with AsyncClient(transport=ASGITransport(app=_app(settings)), base_url="http://test") as c:
        await _sign_in(c, provider)
        first = await c.patch("/api/keys/hashed-1", json={"max_budget": 100}, headers=ORIGIN)
        second = await c.post("/api/keys/hashed-1/unblock", headers=ORIGIN)
        refreshes = provider.refreshes
        third = await c.patch("/api/keys/hashed-1", json={"max_budget": 200}, headers=ORIGIN)
        block = await c.post("/api/keys/hashed-1/block", headers=ORIGIN)
    assert first.status_code == 200 and second.status_code == 200
    assert third.status_code == 429
    assert provider.refreshes == refreshes  # the limited one didn't reach the provider
    assert routes["update"].call_count == 1
    assert block.status_code == 200  # blocking only reduces access: not limited


@pytest.mark.asyncio
@respx.mock
async def test_a_team_whose_alias_is_its_own_id_is_not_ambiguous():
    _mock_provider()
    teams = [{"team_id": "project-alpha", "team_alias": "project-alpha"}]
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=teams))
    provider = Provider(["project-alpha"])
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        me = (await c.get("/api/me")).json()
    assert me["teams"] == [{"team_id": "project-alpha", "team_alias": "project-alpha"}]


def _slow_refresh(provider, started: asyncio.Event):
    """The token endpoint, holding each refresh until the test lets it go."""
    release = asyncio.Event()

    async def endpoint(request):
        if parse_qs(request.content.decode())["grant_type"][0] == "refresh_token":
            started.set()
            await release.wait()
        return provider(request)

    return endpoint, release


@pytest.mark.asyncio
@respx.mock
async def test_a_sign_in_during_a_refresh_keeps_the_new_sign_ins_session():
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS)
    started = asyncio.Event()
    endpoint, release = _slow_refresh(provider, started)
    respx.post(TOKEN_URL).mock(side_effect=endpoint)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        key = asyncio.create_task(c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN))
        await started.wait()  # the refresh with rt-1 is in flight
        provider.refresh_token, provider.groups = "rt-new", ["project-general"]
        await _sign_in(c, provider)  # signs in again meanwhile
        release.set()
        r = await key
        me = (await c.get("/api/me")).json()
        provider.used.clear()
        again = await c.post("/api/keys", json={"name": "k2", "team_id": "project-general"}, headers=ORIGIN)
    assert r.status_code == 401  # the session changed under it
    assert [t["team_id"] for t in me["teams"]] == ["project-general"]  # the new sign-in's groups
    assert again.status_code == 201 and "rt-new" in provider.used  # and its refresh token
    assert generate.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_a_logout_during_a_refresh_stays_logged_out():
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS)
    started = asyncio.Event()
    endpoint, release = _slow_refresh(provider, started)
    respx.post(TOKEN_URL).mock(side_effect=endpoint)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        key = asyncio.create_task(c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN))
        await started.wait()
        await c.post("/api/auth/logout", headers=ORIGIN, follow_redirects=False)
        release.set()
        r = await key
        me = await c.get("/api/me")
    assert r.status_code == 401
    assert me.status_code == 401  # nothing wrote the session back
    assert not generate.called


@pytest.mark.asyncio
@respx.mock
async def test_a_failed_refresh_never_logs_the_token_endpoints_body(caplog):
    _mock_provider()
    _litellm()
    provider = Provider(GROUPS)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        respx.post(TOKEN_URL).mock(side_effect=provider)
        await _sign_in(c, provider)
        # A success the app can't read that still holds tokens: form-encoded.
        respx.post(TOKEN_URL).mock(return_value=Response(
            200, content=b"access_token=at-secret&refresh_token=rt-secret",
            headers={"content-type": "application/x-www-form-urlencoded"},
        ))
        with caplog.at_level("ERROR", logger="app.core.team_claims"):
            r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
    assert r.status_code == 502
    assert "Token refresh failed" in caplog.text and "x-www-form-urlencoded" in caplog.text
    assert "secret" not in caplog.text


def test_startup_refuses_claims_without_a_group_prefix():
    from app.main import _enforce_startup_safety

    for prefix in ("", "  "):
        with pytest.raises(RuntimeError, match="TEAM_GROUP_PREFIX"):
            _enforce_startup_safety(_claims_settings(AUTH_MODE="oauth", TEAM_GROUP_PREFIX=prefix))


@pytest.mark.asyncio
@respx.mock
async def test_a_team_matched_by_id_reports_its_own_alias():
    _mock_provider()
    teams = [{"team_id": "project-alpha", "team_alias": "Alpha"}]
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=teams))
    provider = Provider(["project-alpha"])
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        me = (await c.get("/api/me")).json()
    assert me["teams"] == [{"team_id": "project-alpha", "team_alias": "Alpha"}]



@pytest.mark.parametrize("value", [7, True, {"project-alpha": 1}, None])
def test_a_groups_claim_that_isnt_a_list_or_string_is_no_groups(value):
    assert team_claims.groups_from_claims({"groups": value}, _claims_settings()) == []


@pytest.mark.asyncio
@respx.mock
async def test_a_refused_refresh_after_a_newer_sign_in_keeps_that_sign_in(audited):
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(GROUPS)
    started = asyncio.Event()
    endpoint, release = _slow_refresh(provider, started)
    respx.post(TOKEN_URL).mock(side_effect=endpoint)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        key = asyncio.create_task(c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN))
        await started.wait()
        provider.refresh_token = "rt-new"
        await _sign_in(c, provider)  # signs in again meanwhile
        provider.refresh_status = 400  # and the old token's refresh is refused
        release.set()
        r = await key
        me = await c.get("/api/me")
    assert r.status_code == 401 and "changed" in r.json()["detail"]
    assert me.status_code == 200  # the newer sign-in's session is intact
    assert "oauth_refresh_refused" not in [e for e, _ in audited]
    assert not generate.called


@pytest.mark.asyncio
@respx.mock
async def test_groups_overage_without_a_refresh_token_is_refused_and_audited(audited):
    _mock_provider()
    _, generate = _litellm()
    provider = Provider(None, refresh_token=None)  # no refresh token: the sign-in's groups count
    provider.extra = OVERAGE
    respx.post(TOKEN_URL).mock(side_effect=provider)
    async with AsyncClient(transport=ASGITransport(app=_app(_claims_settings())), base_url="http://test") as c:
        await _sign_in(c, provider)
        r = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
    assert r.status_code == 403
    assert "team_claims_overage" in [e for e, _ in audited]
    assert not generate.called
