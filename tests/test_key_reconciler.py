"""Blocking keys of people no longer in a project's group (key_reconciler.py)."""
import json

import pytest
import respx
from httpx import Response

from app.core import key_reconciler
from app.core.config import Settings
from app.core.entra_directory import DirectoryError
from app.core.litellm_client import LiteLLMClient

LITELLM = "http://mock-litellm:4000"
GROUP = "11111111-2222-3333-4444-555555555555"
ALICE = {"id": "a-oid", "mail": "Alice@Example.com", "userPrincipalName": "alice@example.com"}
BOB = {"id": "b-oid", "mail": "bob@example.com", "userPrincipalName": "bob@example.com"}


def _settings(**overrides) -> Settings:
    base = dict(
        LITELLM_BASE_URL=LITELLM,
        LITELLM_ADMIN_KEY="sk-test-admin-key",
        FEATURE_TEAMS_ENABLED=True,
        TEAM_SOURCE="claims",
        AUTH_MODE="oauth",
        TEAM_GROUP_PREFIX="genesis-",
        RECONCILE_ENABLED=True,
        RECONCILE_DRY_RUN=False,
    )
    base.update(overrides)
    return Settings(**base)


class Directory:
    """Members by group or role name; an exception instance means Graph failed."""

    def __init__(self, groups=None, roles=None):
        self.groups, self.roles = groups or {}, roles or {}
        self.asked = []

    async def group_members(self, name):
        self.asked.append(("group", name))
        return self._answer(self.groups.get(name))

    async def app_role_members(self, name):
        self.asked.append(("role", name))
        return self._answer(self.roles.get(name))

    @staticmethod
    def _answer(value):
        if isinstance(value, Exception):
            raise value
        return value


def _key(token, user, team="t-mag", blocked=False):
    return {"token": token, "user_id": user, "team_id": team, "blocked": blocked, "key_alias": f"{user}-k"}


def _litellm(teams, keys):
    """LiteLLM with ``teams`` and ``keys`` (filtered by ?team_id=); returns the block route."""
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=teams))

    def key_list(request):
        team = request.url.params.get("team_id")
        return Response(200, json={"keys": [k for k in keys if team is None or k["team_id"] == team],
                                   "total_pages": 1})

    respx.get(f"{LITELLM}/key/list").mock(side_effect=key_list)
    return respx.post(f"{LITELLM}/key/block").mock(return_value=Response(200, json={}))


@pytest.fixture
def audited(monkeypatch):
    events = []
    monkeypatch.setattr(key_reconciler, "audit", lambda event, **f: events.append((event, f)))
    return events


def _blocked_tokens(route):
    return [json.loads(c.request.content)["key"] for c in route.calls]


@pytest.mark.asyncio
@respx.mock
async def test_blocks_keys_of_people_not_in_the_group(audited):
    block = _litellm(
        [{"team_id": "t-mag", "team_alias": "genesis-mag-users"}, {"team_id": "t-other", "team_alias": "staff"}],
        [_key("k1", "alice@example.com"), _key("k2", "carol@example.com"),
         _key("k3", "carol@example.com", blocked=True), {"token": "k4", "user_id": None, "team_id": "t-mag"},
         _key("k5", "dave@example.com", team="t-other")],
    )
    s = _settings()
    result = await key_reconciler.reconcile_once(s, LiteLLMClient(s), Directory(groups={"genesis-mag-users": [ALICE]}))
    assert _blocked_tokens(block) == ["k2"]  # not Alice (case), not already blocked, not ownerless
    assert result["blocked"] == 1
    assert ("reconcile_block", {"user": "carol@example.com", "team_id": "t-mag", "key_alias": "carol@example.com-k"}) in audited


@pytest.mark.asyncio
@respx.mock
async def test_dry_run_blocks_nothing(audited):
    block = _litellm([{"team_id": "t-mag", "team_alias": "genesis-mag-users"}], [_key("k2", "carol@example.com")])
    s = _settings(RECONCILE_DRY_RUN=True)
    result = await key_reconciler.reconcile_once(s, LiteLLMClient(s), Directory(groups={"genesis-mag-users": [ALICE]}))
    assert not block.called
    assert result["to_block"] == 1 and result["blocked"] == 0
    assert any(e == "reconcile_would_block" for e, _ in audited)


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("answer", [None, [], DirectoryError("Graph down"), [{**BOB, "accountEnabled": False}]])
async def test_a_group_that_cant_be_read_changes_nothing(audited, answer):
    block = _litellm([{"team_id": "t-mag", "team_alias": "genesis-mag-users"}], [_key("k2", "carol@example.com")])
    s = _settings()
    result = await key_reconciler.reconcile_once(s, LiteLLMClient(s), Directory(groups={"genesis-mag-users": answer}))
    assert not block.called
    assert result["skipped"] == 1
    assert any(e == "reconcile_skip_team" for e, _ in audited)


@pytest.mark.asyncio
@respx.mock
async def test_a_disabled_account_is_not_a_member():
    block = _litellm([{"team_id": "t-mag", "team_alias": "genesis-mag-users"}],
                     [_key("k1", "alice@example.com"), _key("k2", "bob@example.com")])
    s = _settings()
    members = [ALICE, {**BOB, "accountEnabled": False}]
    await key_reconciler.reconcile_once(s, LiteLLMClient(s), Directory(groups={"genesis-mag-users": members}))
    assert _blocked_tokens(block) == ["k2"]


@pytest.mark.asyncio
@respx.mock
async def test_a_name_two_teams_share_is_skipped():
    block = _litellm(
        [{"team_id": "t1", "team_alias": "genesis-mag-users"}, {"team_id": "t2", "team_alias": "genesis-mag-users"}],
        [_key("k2", "carol@example.com", team="t1")],
    )
    s = _settings()
    result = await key_reconciler.reconcile_once(s, LiteLLMClient(s), Directory(groups={"genesis-mag-users": [ALICE]}))
    assert not block.called and result["skipped"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_more_blocks_than_the_limit_blocks_none(audited):
    keys = [_key(f"k{i}", f"user{i}@example.com") for i in range(3)]
    block = _litellm([{"team_id": "t-mag", "team_alias": "genesis-mag-users"}], keys)
    s = _settings(RECONCILE_MAX_BLOCKS_PER_RUN=2)
    result = await key_reconciler.reconcile_once(s, LiteLLMClient(s), Directory(groups={"genesis-mag-users": [ALICE]}))
    assert not block.called and result["refused"] is True
    assert any(e == "reconcile_refused" for e, _ in audited)


@pytest.mark.asyncio
@respx.mock
async def test_object_id_teams_and_oid_user_ids():
    # The group's object ID as team_id and prefix; people keyed by their oid.
    block = _litellm([{"team_id": GROUP, "team_alias": "genesis-mag-users"}],
                     [_key("k1", "a-oid", team=GROUP), _key("k2", "x-oid", team=GROUP)])
    s = _settings(TEAM_GROUP_PREFIX=GROUP, OAUTH_USER_ID_FIELD="oid")
    directory = Directory(groups={GROUP: [ALICE]})
    await key_reconciler.reconcile_once(s, LiteLLMClient(s), directory)
    assert directory.asked == [("group", GROUP)]
    assert _blocked_tokens(block) == ["k2"]


@pytest.mark.asyncio
@respx.mock
async def test_roles_mode_reads_app_role_members():
    block = _litellm([{"team_id": "t-mag", "team_alias": "genesis-mag-users"}],
                     [_key("k1", "alice@example.com"), _key("k2", "carol@example.com")])
    s = _settings(GROUPS_FIELD="roles")
    directory = Directory(roles={"genesis-mag-users": [ALICE]})
    await key_reconciler.reconcile_once(s, LiteLLMClient(s), directory)
    assert directory.asked == [("role", "genesis-mag-users")]
    assert _blocked_tokens(block) == ["k2"]


@pytest.mark.asyncio
@respx.mock
async def test_keys_without_a_team_only_when_asked_and_only_when_every_project_was_read():
    teams = [{"team_id": "t-mag", "team_alias": "genesis-mag-users"}, {"team_id": "t-b", "team_alias": "genesis-b"}]
    keys = [_key("k1", "alice@example.com", team=None), _key("k2", "carol@example.com", team=None)]
    block = _litellm(teams, keys)
    both = {"genesis-mag-users": [ALICE], "genesis-b": [BOB]}

    s = _settings()  # off by default
    await key_reconciler.reconcile_once(s, LiteLLMClient(s), Directory(groups=both))
    assert not block.called

    s = _settings(RECONCILE_KEYS_WITHOUT_TEAM=True)
    await key_reconciler.reconcile_once(s, LiteLLMClient(s), Directory(groups={**both, "genesis-b": None}))
    assert not block.called  # one project couldn't be read: unsure who's in none

    await key_reconciler.reconcile_once(s, LiteLLMClient(s), Directory(groups=both))
    assert _blocked_tokens(block) == ["k2"]  # Alice is in a project; Carol in none


@pytest.mark.parametrize("overrides, message", [
    ({"TEAM_SOURCE": "litellm"}, "TEAM_SOURCE=claims"),
    ({"GRAPH_TENANT_ID": ""}, "GRAPH_TENANT_ID"),
    ({"RECONCILE_INTERVAL_SECONDS": 30}, "at least 60"),
    ({"OAUTH_USER_ID_FIELD": "sub"}, "sub"),
])
def test_startup_refuses_an_unsafe_reconciler(overrides, message):
    from app.main import _enforce_startup_safety
    from tests.test_oidc import _settings as oauth_settings

    base = dict(FEATURE_TEAMS_ENABLED=True, TEAM_SOURCE="claims", RECONCILE_ENABLED=True,
                GRAPH_TENANT_ID="tenant", OAUTH_USER_ID_FIELD="oid")
    with pytest.raises(RuntimeError, match=message):
        _enforce_startup_safety(oauth_settings(**{**base, **overrides}))


def test_startup_accepts_a_configured_reconciler():
    from app.main import _enforce_startup_safety
    from tests.test_oidc import _settings as oauth_settings

    _enforce_startup_safety(oauth_settings(
        FEATURE_TEAMS_ENABLED=True, TEAM_SOURCE="claims", RECONCILE_ENABLED=True,
        GRAPH_TENANT_ID="tenant", OAUTH_USER_ID_FIELD="oid",
    ))


@pytest.mark.asyncio
async def test_the_app_runs_the_reconciler_in_the_background(monkeypatch):
    import asyncio

    import app.core.config as config_mod
    import app.main as main_mod
    from tests.test_oidc import _settings as oauth_settings

    started, stopped = asyncio.Event(), asyncio.Event()

    async def loop(settings):
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            stopped.set()

    monkeypatch.setattr(key_reconciler, "run_periodically", loop)
    monkeypatch.setattr(config_mod, "settings", oauth_settings(
        FEATURE_TEAMS_ENABLED=True, TEAM_SOURCE="claims", RECONCILE_ENABLED=True,
        GRAPH_TENANT_ID="tenant", OAUTH_USER_ID_FIELD="oid",
    ))
    app = main_mod.create_app()
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), 1)
    assert stopped.is_set()


@pytest.mark.asyncio
@respx.mock
async def test_one_pass_outside_the_app(monkeypatch):
    seen = {}

    async def once(settings, client, directory):
        seen["directory"] = directory
        return {"blocked": 0}

    monkeypatch.setattr(key_reconciler, "reconcile_once", once)
    assert await key_reconciler.run_once_from_settings(_settings(GRAPH_TENANT_ID="t")) == {"blocked": 0}
    assert seen["directory"].tenant == "t"
