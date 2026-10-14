"""Team membership against a real LiteLLM proxy (not the bundled mock).

Skipped unless LITELLM_INTEGRATION_URL and LITELLM_INTEGRATION_KEY (an admin
key) are set. Run against a staging proxy:

    LITELLM_INTEGRATION_URL=http://localhost:4000 LITELLM_INTEGRATION_KEY=sk-... \\
      uv run --extra dev pytest tests/test_litellm_integration.py -v

Each test creates a throwaway team (``probe-<uuid>``, no models) and user, and
deletes them afterwards.
"""
import os
import uuid

import httpx
import pytest

from app.core import team_claims
from app.core.config import Settings
from app.core.litellm_client import LiteLLMClient

URL = os.environ.get("LITELLM_INTEGRATION_URL", "").rstrip("/")
KEY = os.environ.get("LITELLM_INTEGRATION_KEY", "")

pytestmark = pytest.mark.skipif(
    not (URL and KEY), reason="LITELLM_INTEGRATION_URL and LITELLM_INTEGRATION_KEY not set"
)


@pytest.fixture
async def probe():
    """A team and a user of its own, removed afterwards whatever happens."""
    admin = httpx.AsyncClient(base_url=URL, headers={"Authorization": f"Bearer {KEY}"}, timeout=30)
    name = f"probe-{uuid.uuid4()}"
    user_id = f"{name}-user"
    team_id = None
    client = LiteLLMClient(Settings(LITELLM_BASE_URL=URL, LITELLM_ADMIN_KEY=KEY))
    team_claims.reset_cache()
    try:
        r = await admin.post("/team/new", json={"team_alias": name, "models": ["no-default-models"]})
        r.raise_for_status()
        team_id = r.json()["team_id"]
        await client.ensure_user(user_id)
        yield client, admin, name, team_id, user_id
    finally:
        if team_id:
            await admin.post("/team/delete", json={"team_ids": [team_id]})
        await admin.post("/user/delete", json={"user_ids": [user_id]})
        await admin.aclose()
        # The client's pool is shared per URL; each test runs on its own event loop.
        shared = LiteLLMClient._shared.pop(URL, None)
        if shared is not None:
            await shared.aclose()
        team_claims.reset_cache()


def _in(team_id, teams):
    return team_id in {t.get("team_id") for t in teams}


async def test_member_add_is_idempotent(probe):
    client, _, _, team_id, user_id = probe
    assert not _in(team_id, await client.list_teams(user_id))
    assert await client.ensure_team_member(team_id, user_id) is True  # added
    assert _in(team_id, await client.list_teams(user_id))
    # Again: no error, no second membership, and not reported as added.
    assert await client.ensure_team_member(team_id, user_id) is False
    teams = await client.list_teams(user_id)
    assert [t.get("team_id") for t in teams].count(team_id) == 1


async def test_team_lookup_by_group_name(probe):
    client, _, name, team_id, _ = probe
    teams = await team_claims.teams_for_groups([name], client)
    assert teams == [{"team_id": team_id, "team_alias": name}]


async def test_member_delete_removes_their_keys_in_the_team(probe):
    """What a reconciler relies on: taking someone out of a team deletes their
    keys in it, so membership changes reach existing keys."""
    client, admin, name, team_id, user_id = probe
    await client.ensure_team_member(team_id, user_id)
    key = (await client.generate_key(user_id, f"{name}-key", team_id=team_id))["key"]
    assert (await client.get_key_info(key))["info"]["team_id"] == team_id
    r = await admin.post("/team/member_delete", json={"team_id": team_id, "user_id": user_id})
    r.raise_for_status()
    assert not _in(team_id, await client.list_teams(user_id))
    with pytest.raises(httpx.HTTPStatusError) as e:
        await client.get_key_info(key)
    assert e.value.response.status_code == 404
