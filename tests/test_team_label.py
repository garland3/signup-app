"""TEAM_LABEL: the deployment's word for a team in the UI and messages."""
import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from tests.conftest import create_test_app
from tests.test_oidc import _mock_provider
from tests.test_team_claims import (
    GROUPS,
    ORIGIN,
    TOKEN_URL,
    Provider,
    _app,
    _claims_settings,
    _litellm,
    _sign_in,
)

AUTH = {"X-User-Email": "alice@example.com"}
LITELLM = "http://mock-litellm:4000"


@pytest.fixture
def project_app(monkeypatch):
    app = create_test_app(teams_enabled=True)
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "TEAM_LABEL", "Project")  # undone after the test
    return app


@pytest.mark.asyncio
async def test_config_reports_the_label(project_app):
    async with AsyncClient(transport=ASGITransport(app=project_app), base_url="http://test") as c:
        r = await c.get("/api/config", headers=AUTH)
    assert (r.json()["team_label"], r.json()["team_label_plural"]) == ("Project", "Projects")


@pytest.mark.asyncio
async def test_default_label_is_team():
    app = create_test_app(teams_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/config", headers=AUTH)
    assert (r.json()["team_label"], r.json()["team_label_plural"]) == ("Team", "Teams")


@pytest.mark.asyncio
@respx.mock
async def test_messages_use_the_label(project_app):
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=[]))
    async with AsyncClient(transport=ASGITransport(app=project_app), base_url="http://test") as c:
        missing = await c.post("/api/keys", json={"name": "k"}, headers=AUTH)
        invalid = await c.post("/api/keys", json={"name": "k", "team_id": "t-x"}, headers=AUTH)
    assert missing.json()["detail"] == "Keys belong to projects: join one before creating a key."
    assert invalid.json()["detail"] == "Invalid project for this user"


@pytest.mark.asyncio
@respx.mock
async def test_claims_messages_use_the_label():
    _mock_provider()
    _litellm()
    provider = Provider(GROUPS)
    respx.post(TOKEN_URL).mock(side_effect=provider)
    app = _app(_claims_settings(TEAM_LABEL="Project"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await _sign_in(c, provider)
        r = await c.post("/api/keys", json={"name": "k"}, headers=ORIGIN)
        provider.refresh_status = 503
        down = await c.post("/api/keys", json={"name": "k", "team_id": "t-alpha"}, headers=ORIGIN)
    assert r.json()["detail"].startswith("Keys belong to projects, and yours come from your sign-in")
    assert down.status_code == 502 and down.json()["detail"] == "Could not check your projects; try again"


@pytest.mark.parametrize(
    "label, plural, as_written, in_a_sentence",
    [
        ("Project", "", ("Project", "Projects"), ("project", "projects")),
        ("Class", "Classes", ("Class", "Classes"), ("class", "classes")),  # an irregular plural
        ("SIG", "", ("SIG", "SIGs"), ("SIG", "SIGs")),  # an acronym keeps its case
        ("Cost Center", "", ("Cost Center", "Cost Centers"), ("Cost Center", "Cost Centers")),
        ("  ", "", ("Team", "Teams"), ("team", "teams")),  # blank: the default
    ],
)
def test_label_forms(label, plural, as_written, in_a_sentence):
    from app.core.config import Settings

    s = Settings(TEAM_LABEL=label, TEAM_LABEL_PLURAL=plural)
    assert (s.team_label(), s.team_label(plural=True)) == as_written
    assert (s.team_word(), s.team_word(plural=True)) == in_a_sentence


@pytest.mark.asyncio
@respx.mock
async def test_join_messages_use_the_label(project_app):
    alpha = {"team_id": "team-alpha", "team_alias": "Alpha"}
    respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=[]))
    respx.get(f"{LITELLM}/team/available").mock(return_value=Response(200, json={"available_teams": []}))
    async with AsyncClient(transport=ASGITransport(app=project_app), base_url="http://test") as c:
        unavailable = await c.post("/api/teams/join", json={"team_id": "team-alpha"}, headers=AUTH)
        respx.get(f"{LITELLM}/team/list").mock(return_value=Response(200, json=[alpha]))
        member = await c.post("/api/teams/join", json={"team_id": "team-alpha"}, headers=AUTH)
    assert (unavailable.status_code, unavailable.json()["detail"]) == (403, "Project not available to join")
    assert (member.status_code, member.json()["detail"]) == (409, "You are already a member of this project.")
