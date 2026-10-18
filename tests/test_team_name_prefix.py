"""HIDE_TEAM_NAME_PREFIX: team names shown without TEAM_GROUP_PREFIX."""
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from tests.test_project_task import _activity, _dashboard

AUTH = {"X-User-Email": "alice@example.com"}


@pytest.mark.parametrize("hide, prefix, name, shown", [
    (True, "project-", "project-alpha", "alpha"),
    (True, "project-", "team-alpha", "team-alpha"),  # without the prefix: as it is
    (True, "project-", "project-", "project-"),  # nothing after it
    (True, "", "project-alpha", "project-alpha"),  # no prefix to hide
    (False, "project-", "project-alpha", "project-alpha"),  # the default
])
def test_team_display_name(hide, prefix, name, shown):
    s = Settings(HIDE_TEAM_NAME_PREFIX=hide, TEAM_GROUP_PREFIX=prefix)
    assert s.team_display_name(name) == shown


@pytest.mark.parametrize("hide, sent", [(False, ""), (True, "project-")])
@pytest.mark.asyncio
async def test_the_page_gets_the_prefix_to_hide(monkeypatch, hide, sent):
    from app.core.config import get_settings
    from tests.conftest import create_test_app

    app = create_test_app(teams_enabled=True)
    monkeypatch.setattr(get_settings(), "HIDE_TEAM_NAME_PREFIX", hide)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/config", headers=AUTH)
    assert r.json()["team_name_prefix"] == sent


@pytest.mark.parametrize("hide, shown", [(True, "alpha"), (False, "project-alpha")])
@pytest.mark.asyncio
@respx.mock
async def test_the_usage_pages_projects_are_shown_without_it(monkeypatch, hide, shown):
    from app.core.config import get_settings
    from tests.conftest import create_test_app

    app = create_test_app(teams_enabled=True)
    monkeypatch.setattr(get_settings(), "HIDE_TEAM_NAME_PREFIX", hide)
    activity = _activity({"tok-a": ("alice@example.com-a", "team-alpha", 0.10, 1)})
    keys = [{"token": "tok-a", "key_alias": "alice@example.com-a", "metadata": {}}]
    rows = (await _dashboard(app, activity, keys))["project_task_breakdown"]
    assert [(r["project"], r["display_project"]) for r in rows] == [("project-alpha", shown)]


@pytest.mark.asyncio
@respx.mock
async def test_typed_projects_are_shown_as_typed(monkeypatch):
    """Without teams the project is what people entered, not a team: nothing is left off."""
    from app.core.config import get_settings
    from tests.conftest import create_test_app

    app = create_test_app()
    monkeypatch.setattr(get_settings(), "HIDE_TEAM_NAME_PREFIX", True)
    activity = _activity({"tok-a": ("alice@example.com-a", None, 0.10, 1)})
    keys = [{"token": "tok-a", "key_alias": "alice@example.com-a", "metadata": {"project": "project-x"}}]
    rows = (await _dashboard(app, activity, keys))["project_task_breakdown"]
    assert [r["project"] for r in rows] == ["project-x"] and "display_project" not in rows[0]
