"""The optional Task on keys, and the Usage page's key and project/task breakdowns."""
import json
from datetime import UTC, datetime

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import Settings

AUTH = {"X-User-Email": "alice@example.com"}
LITELLM = "http://mock-litellm:4000"
TEAMS = [{"team_id": "team-alpha", "team_alias": "project-alpha"}]


@pytest.fixture
def teams_app():
    from tests.conftest import create_test_app

    return create_test_app(teams_enabled=True)


@pytest.mark.asyncio
@respx.mock
async def test_a_key_is_made_with_its_task(teams_app):
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=TEAMS))
    respx.get(f"{LITELLM}/user/info").mock(return_value=Response(200, json={"user_id": "alice@example.com"}))
    generate = respx.post(f"{LITELLM}/key/generate").mock(
        return_value=Response(200, json={"key": "sk-new", "token_id": "tok-new", "key_alias": "alice@example.com-k"})
    )
    async with AsyncClient(transport=ASGITransport(app=teams_app), base_url="http://test") as c:
        r = await c.post("/api/keys", json={"name": "k", "team_id": "team-alpha", "metadata": {"task": "planning"}},
                         headers=AUTH)
    assert r.status_code == 201
    sent = json.loads(generate.calls[0].request.content)
    # The project is the key's team, so the app writes none.
    assert sent["team_id"] == "team-alpha"
    assert sent["metadata"] == {"task": "planning"}


@pytest.mark.parametrize("required, breakdown, offered", [
    ("", True, True),
    ("project,task_number", True, False),  # already asked for, as a required field
    ("task", True, False),
    ("", False, False),  # no breakdown to group by it
])
def test_the_task_field_is_offered_with_the_breakdown(required, breakdown, offered):
    s = Settings(REQUIRED_KEY_METADATA=required, SHOW_PROJECT_TASK_BREAKDOWN=breakdown)
    assert s.optional_task is offered


@pytest.mark.parametrize("teams, required, is_team", [
    (True, "", True),
    (True, "project,task_number", False),  # people's own project (a charge code)
    (False, "", False),
])
def test_the_project_is_the_team_with_teams(teams, required, is_team):
    s = Settings(FEATURE_TEAMS_ENABLED=teams, REQUIRED_KEY_METADATA=required)
    assert s.project_is_team is is_team


# --- The Usage page ---------------------------------------------------------


def _metrics(spend, requests):
    return {"spend": spend, "api_requests": requests, "successful_requests": requests,
            "total_tokens": 10 * requests}


def _activity(api_keys):
    """One day of usage; ``api_keys`` maps a token to (alias, team ID, spend, requests)."""
    return {
        "results": [{
            "date": datetime.now(UTC).date().isoformat(),
            "metrics": _metrics(sum(v[2] for v in api_keys.values()), sum(v[3] for v in api_keys.values())),
            "breakdown": {
                "models": {},
                "api_keys": {
                    token: {"metrics": _metrics(spend, n), "metadata": {"key_alias": alias, "team_id": team}}
                    for token, (alias, team, spend, n) in api_keys.items()
                },
                "providers": {},
            },
        }],
        "metadata": {"page": 1, "total_pages": 1, "has_more": False},
    }


async def _dashboard(app, activity, keys, teams=TEAMS):
    respx.get(f"{LITELLM}/user/daily/activity").mock(return_value=Response(200, json=activity))
    respx.get(f"{LITELLM}/user/info").mock(return_value=Response(200, json={"user_id": "alice@example.com"}))
    respx.get(f"{LITELLM}/key/list").mock(return_value=Response(200, json={"keys": keys, "total_pages": 1}))
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=teams))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/dashboard", headers=AUTH)
    assert r.status_code == 200
    return r.json()


def _rows(data):
    return {(r["project"], r["task"]): r for r in data["project_task_breakdown"]}


@pytest.mark.asyncio
@respx.mock
async def test_other_tools_keys_are_one_row(teams_app):
    activity = _activity({
        "tok-mine": ("alice@example.com-laptop", "team-alpha", 0.10, 1),
        "tok-mine-gone": ("alice@example.com-old", "team-alpha", 0.20, 2),  # deleted by LiteLLM
        "tok-admin": ("made-by-an-admin", "team-alpha", 0.40, 4),  # listed, not hidden
        "tok-session": ("gw-alice-project-alpha-1", "team-alpha", 1.00, 10),  # hidden, listed
        "tok-session-gone": ("gw-alice-project-alpha-0", "team-alpha", 2.00, 20),  # deleted
    })
    keys = [
        {"token": "tok-mine", "key_alias": "alice@example.com-laptop", "metadata": {}},
        {"token": "tok-admin", "key_alias": "made-by-an-admin", "metadata": {}},
        {"token": "tok-session", "key_alias": "gw-alice-project-alpha-1", "metadata": {"hidden": True}},
    ]
    data = await _dashboard(teams_app, activity, keys)
    assert {k["display_name"] for k in data["keys"]} == {"laptop", "old", "made-by-an-admin"}
    assert data["other_keys"]["key_count"] == 2
    assert data["other_keys"]["spend"] == pytest.approx(3.00)
    assert data["other_keys"]["requests"] == 30


@pytest.mark.asyncio
@respx.mock
async def test_no_other_keys_no_row(teams_app):
    activity = _activity({"tok-mine": ("alice@example.com-laptop", "team-alpha", 0.10, 1)})
    keys = [{"token": "tok-mine", "key_alias": "alice@example.com-laptop", "metadata": {}}]
    assert (await _dashboard(teams_app, activity, keys))["other_keys"] is None


@pytest.mark.asyncio
@respx.mock
async def test_with_teams_the_project_is_the_keys_team(teams_app):
    activity = _activity({
        "tok-a": ("alice@example.com-a", "team-alpha", 0.10, 1),
        "tok-gone": ("gw-alice-project-alpha-0", "team-alpha", 2.00, 20),  # deleted: no metadata
        "tok-left": ("alice@example.com-b", "team-left", 0.50, 5),  # a team she's no longer in
    })
    keys = [{"token": "tok-a", "key_alias": "alice@example.com-a",
             # project metadata doesn't count with teams; the team does
             "metadata": {"project": "elsewhere", "task": "planning"}}]
    rows = _rows(await _dashboard(teams_app, activity, keys))
    assert rows.keys() == {("project-alpha", "planning"), ("project-alpha", ""), ("team-left", "")}
    assert rows[("project-alpha", "planning")]["spend"] == pytest.approx(0.10)
    assert rows[("project-alpha", "")]["spend"] == pytest.approx(2.00)
    assert rows[("team-left", "")]["spend"] == pytest.approx(0.50)


@pytest.mark.asyncio
@respx.mock
async def test_a_required_project_is_grouped_by_what_people_entered(teams_app, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "REQUIRED_KEY_METADATA", "project,task_number")
    activity = _activity({"tok-a": ("alice@example.com-a", "team-alpha", 0.10, 1)})
    keys = [{"token": "tok-a", "key_alias": "alice@example.com-a",
             "metadata": {"project": "1042", "task_number": "3.1.2"}}]
    assert _rows(await _dashboard(teams_app, activity, keys)).keys() == {("1042", "3.1.2")}


@pytest.mark.asyncio
@respx.mock
async def test_without_teams_the_project_is_metadata():
    from tests.conftest import create_test_app

    activity = _activity({
        "tok-a": ("alice@example.com-a", "team-alpha", 0.10, 1),
        "tok-gone": ("alice@example.com-b", "team-alpha", 0.10, 1),  # no metadata: not grouped
    })
    keys = [{"token": "tok-a", "key_alias": "alice@example.com-a", "metadata": {"project": "1042"}}]
    assert _rows(await _dashboard(create_test_app(), activity, keys)).keys() == {("1042", "")}


@pytest.mark.parametrize("metadata, task", [
    ({"task": "planning"}, "planning"),
    ({"task_number": "3.1.2"}, "3.1.2"),  # the earlier name
    ({"task": "planning", "task_number": "3.1.2"}, "planning"),
])
@pytest.mark.asyncio
@respx.mock
async def test_the_task_is_task_else_task_number(teams_app, metadata, task):
    activity = _activity({"tok-a": ("alice@example.com-a", "team-alpha", 0.10, 1)})
    keys = [{"token": "tok-a", "key_alias": "alice@example.com-a", "metadata": metadata}]
    assert _rows(await _dashboard(teams_app, activity, keys)).keys() == {("project-alpha", task)}
