"""End-to-end tests for the /api/dashboard route.

Mocks the LiteLLM proxy with respx and asserts the assembled payload
contains the rollups the frontend depends on. The dashboard backs onto
``/user/daily/activity`` (the same endpoint the LiteLLM admin UI uses)
rather than ``/spend/logs`` — the per-request log scales badly and
trips the read timeout on busy accounts.
"""

from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import AsyncClient, ASGITransport, Response

AUTH = {"X-User-Email": "alice@example.com"}
LITELLM = "http://mock-litellm:4000"


@pytest.fixture
def app():
    from tests.conftest import create_test_app
    return create_test_app()


def _metrics(
    spend=0.0, prompt_tokens=0, completion_tokens=0,
    api_requests=0, successful=None, failed=0,
):
    if successful is None:
        successful = max(0, api_requests - failed)
    return {
        "spend": spend,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "api_requests": api_requests,
        "successful_requests": successful,
        "failed_requests": failed,
    }


def _daily_activity():
    """A small daily-activity payload that exercises every breakdown."""
    today = datetime.now(timezone.utc).date()
    return {
        "results": [
            {
                "date": today.isoformat(),
                "metrics": _metrics(
                    spend=0.10, prompt_tokens=100, completion_tokens=50,
                    api_requests=1,
                ),
                "breakdown": {
                    "models": {
                        "gpt-4o": {
                            "metrics": _metrics(
                                spend=0.10, prompt_tokens=100,
                                completion_tokens=50, api_requests=1,
                            ),
                            "metadata": {},
                        },
                    },
                    "api_keys": {
                        "sk-aaa11111aaaaaaaa": {
                            "metrics": _metrics(
                                spend=0.10, prompt_tokens=100,
                                completion_tokens=50, api_requests=1,
                            ),
                            "metadata": {"key_alias": "alice-prod"},
                        },
                    },
                    "providers": {},
                },
            },
            {
                "date": (today - timedelta(days=1)).isoformat(),
                "metrics": _metrics(
                    spend=0.30, prompt_tokens=200, completion_tokens=100,
                    api_requests=2, failed=1,
                ),
                "breakdown": {
                    "models": {
                        "claude-3-5-sonnet": {
                            "metrics": _metrics(
                                spend=0.30, prompt_tokens=200,
                                completion_tokens=100, api_requests=2,
                                failed=1,
                            ),
                            "metadata": {},
                        },
                    },
                    "api_keys": {
                        "sk-bbb22222bbbbbbbb": {
                            "metrics": _metrics(
                                spend=0.30, prompt_tokens=200,
                                completion_tokens=100, api_requests=2,
                                failed=1,
                            ),
                            "metadata": {"key_alias": "alice-research"},
                        },
                    },
                    "providers": {},
                },
            },
        ],
        "metadata": {
            "total_spend": 0.40,
            "total_api_requests": 3,
            "total_successful_requests": 2,
            "total_failed_requests": 1,
            "page": 1,
            "total_pages": 1,
            "has_more": False,
        },
    }


@pytest.mark.asyncio
@respx.mock
async def test_dashboard_returns_aggregated_payload(app):
    respx.get(f"{LITELLM}/user/daily/activity").mock(
        return_value=Response(200, json=_daily_activity())
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
        return_value=Response(200, json=[])
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
    assert models["gpt-4o"]["requests"] == 1
    assert models["claude-3-5-sonnet"]["spend"] == pytest.approx(0.30)

    # Keys breakdown — aliases come from the breakdown's per-key metadata.
    aliases = {k["alias"] for k in data["keys"]}
    assert "alice-prod" in aliases
    assert "alice-research" in aliases

    # Status codes projected from success/failure counts.
    assert data["status_codes"]["200"] == 2
    assert data["status_codes"]["error"] == 1

    # User email is echoed back so the page can render it.
    assert data["user_email"] == "alice@example.com"


@pytest.mark.asyncio
@respx.mock
async def test_dashboard_survives_missing_user_info(app):
    """A 404 from /user/info shouldn't kill the dashboard."""
    respx.get(f"{LITELLM}/user/daily/activity").mock(
        return_value=Response(200, json=_daily_activity())
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
async def test_dashboard_502_when_daily_activity_unreachable(app):
    """Without the daily-activity payload there's nothing to render."""
    respx.get(f"{LITELLM}/user/daily/activity").mock(
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
async def test_dashboard_bounds_daily_activity_by_period(app):
    """The /user/daily/activity call must be bounded by start_date+end_date.

    Even though the daily endpoint is much cheaper than /spend/logs, we
    still want the proxy to do the date filter so the response is small
    and predictable.
    """
    route = respx.get(f"{LITELLM}/user/daily/activity").mock(
        return_value=Response(200, json={"results": [], "metadata": {}})
    )
    respx.get(f"{LITELLM}/user/info").mock(
        return_value=Response(200, json={"user_id": "alice@example.com"})
    )
    respx.get(f"{LITELLM}/key/list").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/dashboard?period_days=14", headers=AUTH)
    assert r.status_code == 200

    assert route.called
    params = dict(route.calls.last.request.url.params)
    assert params.get("user_id") == "alice@example.com"
    today = datetime.now(timezone.utc).date()
    assert params.get("start_date") == (today - timedelta(days=14)).isoformat()
    assert params.get("end_date") == today.isoformat()


@pytest.mark.asyncio
@respx.mock
async def test_dashboard_follows_pagination(app):
    """When LiteLLM signals has_more, the client must pull every page."""
    today = datetime.now(timezone.utc).date()
    page1 = {
        "results": [{
            "date": today.isoformat(),
            "metrics": _metrics(spend=0.10, api_requests=1),
            "breakdown": {"models": {}, "api_keys": {}, "providers": {}},
        }],
        "metadata": {"page": 1, "total_pages": 2, "has_more": True},
    }
    page2 = {
        "results": [{
            "date": (today - timedelta(days=1)).isoformat(),
            "metrics": _metrics(spend=0.20, api_requests=2),
            "breakdown": {"models": {}, "api_keys": {}, "providers": {}},
        }],
        "metadata": {"page": 2, "total_pages": 2, "has_more": False},
    }

    def _handler(request):
        page = request.url.params.get("page", "1")
        return Response(200, json=page1 if page == "1" else page2)

    route = respx.get(f"{LITELLM}/user/daily/activity").mock(side_effect=_handler)
    respx.get(f"{LITELLM}/user/info").mock(
        return_value=Response(200, json={"user_id": "alice@example.com"})
    )
    respx.get(f"{LITELLM}/key/list").mock(
        return_value=Response(200, json=[])
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/dashboard?period_days=7", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert route.call_count == 2
    assert data["lifetime"]["requests"] == 3
    assert data["lifetime"]["spend"] == pytest.approx(0.30)


@pytest.mark.asyncio
@respx.mock
async def test_dashboard_period_days_validated(app):
    respx.get(f"{LITELLM}/user/daily/activity").mock(
        return_value=Response(200, json={"results": [], "metadata": {}})
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
