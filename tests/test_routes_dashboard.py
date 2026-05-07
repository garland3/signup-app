"""End-to-end tests for the /api/dashboard route.

Mocks the LiteLLM proxy with respx and asserts the assembled payload
contains the rollups the frontend depends on.
"""

import pytest
import respx
from httpx import AsyncClient, ASGITransport, Response

AUTH = {"X-User-Email": "alice@example.com"}
LITELLM = "http://mock-litellm:4000"


@pytest.fixture
def app():
    from tests.conftest import create_test_app
    return create_test_app()


def _spend_logs():
    """A small but representative set of LiteLLM spend log rows.

    The values are deliberately easy to hand-verify so the assertions
    below stay readable as additions to the dashboard schema accrue.
    """
    return [
        {
            "request_id": "r1",
            "api_key": "sk-aaa11111aaaaaaaa",
            "model": "gpt-4o",
            "spend": 0.10,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "startTime": "2026-05-01T10:00:00Z",
            "user": "alice@example.com",
            "metadata": {"status": "success", "status_code": 200},
            "request_tags": ["project:1042", "task:3.1.2"],
        },
        {
            "request_id": "r2",
            "api_key": "sk-bbb22222bbbbbbbb",
            "model": "claude-3-5-sonnet",
            "spend": 0.30,
            "prompt_tokens": 200,
            "completion_tokens": 100,
            "total_tokens": 300,
            "startTime": "2026-05-02T10:00:00Z",
            "user": "alice@example.com",
            "metadata": {"status": "success", "status_code": 200},
            "request_tags": ["project:2001", "task:1.1"],
        },
        {
            "request_id": "r3",
            "api_key": "sk-aaa11111aaaaaaaa",
            "model": "gpt-4o",
            "spend": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "startTime": "2026-05-03T10:00:00Z",
            "user": "alice@example.com",
            "metadata": {"status": "failure", "status_code": 500},
            "request_tags": ["project:1042"],
        },
    ]


@pytest.mark.asyncio
@respx.mock
async def test_dashboard_returns_aggregated_payload(app):
    respx.get(f"{LITELLM}/spend/logs").mock(
        return_value=Response(200, json=_spend_logs())
    )
    respx.get(f"{LITELLM}/user/info").mock(
        return_value=Response(200, json={
            "user_id": "alice@example.com",
            "spend": 0.40,
            "max_budget": 10.0,
            "soft_budget": 8.0,
            "budget_duration": "30d",
            "budget_reset_at": "2026-06-01T00:00:00Z",
        })
    )
    respx.get(f"{LITELLM}/key/list").mock(
        return_value=Response(200, json=[
            {
                "token": "sk-aaa11111aaaaaaaa",
                "key_alias": "alice-prod",
                "user_id": "alice@example.com",
            },
            {
                "token": "sk-bbb22222bbbbbbbb",
                "key_alias": "alice-research",
                "user_id": "alice@example.com",
            },
        ])
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/dashboard", headers=AUTH)

    assert r.status_code == 200
    data = r.json()

    # Budget posture
    assert data["budget"]["max_budget"] == 10.0
    assert data["budget"]["spend"] == pytest.approx(0.4)
    assert data["budget"]["remaining"] == pytest.approx(9.6)
    assert data["budget"]["consumed_pct"] == pytest.approx(4.0)

    # Lifetime totals
    assert data["lifetime"]["requests"] == 3
    assert data["lifetime"]["successful_requests"] == 2
    assert data["lifetime"]["failed_requests"] == 1
    assert data["lifetime"]["total_tokens"] == 450

    # Models breakdown
    models = {m["key"]: m for m in data["models"]}
    assert "gpt-4o" in models
    assert "claude-3-5-sonnet" in models
    assert models["gpt-4o"]["requests"] == 2
    assert models["claude-3-5-sonnet"]["spend"] == pytest.approx(0.30)

    # Keys breakdown picks up aliases from /key/list.
    aliases = {k["alias"] for k in data["keys"]}
    assert "alice-prod" in aliases
    assert "alice-research" in aliases

    # Project / task hierarchy
    projects = {p["project"]: p for p in data["projects"]}
    assert "1042" in projects
    assert "2001" in projects
    assert projects["1042"]["requests"] == 2

    # Status codes
    assert data["status_codes"]["200"] == 2
    assert data["status_codes"]["500"] == 1

    # User email is echoed back so the page can render it.
    assert data["user_email"] == "alice@example.com"


@pytest.mark.asyncio
@respx.mock
async def test_dashboard_survives_missing_user_info(app):
    """A 404 from /user/info shouldn't kill the dashboard.

    Newly-onboarded users may not have a user record yet; the spend
    rollups still work without one.
    """
    respx.get(f"{LITELLM}/spend/logs").mock(
        return_value=Response(200, json=_spend_logs())
    )
    respx.get(f"{LITELLM}/user/info").mock(
        return_value=Response(404, json={"detail": "User not found"})
    )
    respx.get(f"{LITELLM}/key/list").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/dashboard", headers=AUTH)

    assert r.status_code == 200
    data = r.json()
    assert data["budget"]["max_budget"] is None
    assert data["lifetime"]["requests"] == 3


@pytest.mark.asyncio
@respx.mock
async def test_dashboard_502_when_spend_logs_unreachable(app):
    """Without spend logs there's nothing to render, so we surface a 502."""
    respx.get(f"{LITELLM}/spend/logs").mock(
        return_value=Response(503, json={"detail": "down"})
    )
    respx.get(f"{LITELLM}/user/info").mock(
        return_value=Response(200, json={"user_id": "alice@example.com"})
    )
    respx.get(f"{LITELLM}/key/list").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/dashboard", headers=AUTH)

    assert r.status_code == 502


@pytest.mark.asyncio
async def test_dashboard_requires_auth(app):
    """No X-User-Email header -> 401, just like every other /api route."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/dashboard")
    assert r.status_code == 401


@pytest.mark.asyncio
@respx.mock
async def test_dashboard_period_days_validated(app):
    respx.get(f"{LITELLM}/spend/logs").mock(
        return_value=Response(200, json=[])
    )
    respx.get(f"{LITELLM}/user/info").mock(
        return_value=Response(200, json={"user_id": "alice@example.com"})
    )
    respx.get(f"{LITELLM}/key/list").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # Reject out-of-range values.
        r = await c.get("/api/dashboard?period_days=0", headers=AUTH)
        assert r.status_code == 422
        r = await c.get("/api/dashboard?period_days=400", headers=AUTH)
        assert r.status_code == 422
        # Accept a valid value.
        r = await c.get("/api/dashboard?period_days=7", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["period_days"] == 7
