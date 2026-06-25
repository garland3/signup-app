"""Unit tests for the dashboard aggregation pipeline.

The aggregator consumes LiteLLM's ``/user/daily/activity`` response —
one entry per day with per-model and per-api_key sub-totals already
rolled up. These tests cover:

- Lifetime and current-period totals (and the window filter that
  separates them).
- Per-model and per-key breakdowns, including the alias backfill from
  ``/key/list`` when the proxy doesn't supply one.
- Status code projection (success/failure counts → ``200``/``error``
  buckets).
- Budget posture (``remaining``, ``consumed_pct``, soft threshold).
- Time-series gap-fill (zero-filled days inside the span).
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.dashboard_metrics import aggregate, build_budget


def _metrics(
    spend=0.0,
    prompt_tokens=0,
    completion_tokens=0,
    total_tokens=None,
    api_requests=0,
    successful_requests=None,
    failed_requests=0,
):
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens
    if successful_requests is None:
        successful_requests = max(0, api_requests - failed_requests)
    return {
        "spend": spend,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "api_requests": api_requests,
        "successful_requests": successful_requests,
        "failed_requests": failed_requests,
    }


def _day(date_str, metrics, *, models=None, api_keys=None):
    return {
        "date": date_str,
        "metrics": metrics,
        "breakdown": {
            "models": models or {},
            "api_keys": api_keys or {},
            "providers": {},
        },
    }


def _activity(results, *, total_pages=1, has_more=False):
    return {
        "results": results,
        "metadata": {
            "page": 1,
            "total_pages": total_pages,
            "has_more": has_more,
        },
    }


# ---------------------------------------------------------------------------
# Totals + per-dimension breakdowns
# ---------------------------------------------------------------------------


def test_aggregate_sums_lifetime_totals_across_days():
    today = datetime.now(timezone.utc).date()
    results = [
        _day(
            today.isoformat(),
            _metrics(spend=0.30, prompt_tokens=200, completion_tokens=100, api_requests=2),
        ),
        _day(
            (today - timedelta(days=1)).isoformat(),
            _metrics(
                spend=0.10, prompt_tokens=100, completion_tokens=50,
                api_requests=2, failed_requests=1,
            ),
        ),
    ]
    out = aggregate(
        user_info=None, daily_activity=_activity(results), period_days=30
    )
    assert out["lifetime"]["spend"] == pytest.approx(0.40)
    assert out["lifetime"]["total_tokens"] == 450
    assert out["lifetime"]["requests"] == 4
    assert out["lifetime"]["successful_requests"] == 3
    assert out["lifetime"]["failed_requests"] == 1


def test_aggregate_breaks_down_by_model_with_cost_per_token():
    today = datetime.now(timezone.utc).date()
    results = [
        _day(
            today.isoformat(),
            _metrics(spend=0.45, prompt_tokens=200, completion_tokens=100, api_requests=3),
            models={
                "gpt-4o": {
                    "metrics": _metrics(
                        spend=0.30, prompt_tokens=200, completion_tokens=100,
                        total_tokens=3000, api_requests=2,
                    ),
                    "metadata": {},
                },
                "claude-3-5-sonnet": {
                    "metrics": _metrics(
                        spend=0.15, total_tokens=500, api_requests=1,
                    ),
                    "metadata": {},
                },
            },
        )
    ]
    out = aggregate(user_info=None, daily_activity=_activity(results))
    by_model = {m["key"]: m for m in out["models"]}
    assert by_model["gpt-4o"]["spend"] == pytest.approx(0.30)
    assert by_model["gpt-4o"]["total_tokens"] == 3000
    assert by_model["gpt-4o"]["cost_per_token"] == pytest.approx(0.0001)
    # Sorted by spend descending.
    assert out["models"][0]["key"] == "gpt-4o"


def test_aggregate_keys_breakdown_uses_alias_from_breakdown_metadata():
    today = datetime.now(timezone.utc).date()
    results = [
        _day(
            today.isoformat(),
            _metrics(spend=2.0, total_tokens=300, api_requests=2),
            api_keys={
                "sk-realkey1234": {
                    "metrics": _metrics(spend=2.0, total_tokens=300, api_requests=2),
                    "metadata": {"key_alias": "alice-prod"},
                }
            },
        )
    ]
    out = aggregate(user_info=None, daily_activity=_activity(results))
    assert len(out["keys"]) == 1
    entry = out["keys"][0]
    assert entry["alias"] == "alice-prod"
    assert entry["spend"] == pytest.approx(2.0)
    assert entry["requests"] == 2
    assert entry["key_prefix"] == "sk-realk..."


def test_aggregate_keys_backfills_alias_from_key_list_when_missing():
    today = datetime.now(timezone.utc).date()
    results = [
        _day(
            today.isoformat(),
            _metrics(spend=1.0, api_requests=1),
            api_keys={
                "sk-realkey1234": {
                    "metrics": _metrics(spend=1.0, api_requests=1),
                    "metadata": {},
                }
            },
        )
    ]
    keys_list = [{"token": "sk-realkey1234", "key_alias": "alice-prod"}]
    out = aggregate(
        user_info=None, daily_activity=_activity(results), keys_list=keys_list,
    )
    assert out["keys"][0]["alias"] == "alice-prod"


def test_aggregate_project_task_breakdown_from_key_metadata():
    today = datetime.now(timezone.utc).date()
    results = [
        _day(
            today.isoformat(),
            _metrics(spend=3.0, total_tokens=600, api_requests=3),
            api_keys={
                "sk-project1": {
                    "metrics": _metrics(spend=1.0, total_tokens=200, api_requests=1),
                    "metadata": {
                        "project": "phoenix",
                        "task_number": "T-42",
                    },
                },
                "sk-project2": {
                    "metrics": _metrics(spend=2.0, total_tokens=400, api_requests=2),
                    "metadata": {
                        "project": "phoenix",
                        "task_number": "T-43",
                    },
                },
            },
        )
    ]
    out = aggregate(user_info=None, daily_activity=_activity(results))
    by_task = {row["task_number"]: row for row in out["project_task_breakdown"]}
    assert by_task["T-42"]["project"] == "phoenix"
    assert by_task["T-42"]["spend"] == pytest.approx(1.0)
    assert by_task["T-43"]["requests"] == 2


def test_aggregate_project_task_breakdown_backfills_from_key_list_metadata():
    today = datetime.now(timezone.utc).date()
    results = [
        _day(
            today.isoformat(),
            _metrics(spend=1.0, total_tokens=100, api_requests=1),
            api_keys={
                "sk-fallback-token": {
                    "metrics": _metrics(spend=1.0, total_tokens=100, api_requests=1),
                    "metadata": {},
                },
            },
        )
    ]
    keys_list = [{
        "token": "sk-fallb...",
        "metadata": {"project": "atlas", "task_number": "A-1"},
    }]
    out = aggregate(
        user_info=None, daily_activity=_activity(results), keys_list=keys_list,
    )
    assert out["project_task_breakdown"][0]["project"] == "atlas"
    assert out["project_task_breakdown"][0]["task_number"] == "A-1"


# ---------------------------------------------------------------------------
# Status code projection
# ---------------------------------------------------------------------------


def test_status_codes_project_success_and_failure_counts():
    today = datetime.now(timezone.utc).date()
    results = [
        _day(
            today.isoformat(),
            _metrics(api_requests=5, failed_requests=2),
        )
    ]
    out = aggregate(user_info=None, daily_activity=_activity(results))
    assert out["status_codes"] == {"200": 3, "error": 2}


def test_status_codes_omit_zero_buckets():
    today = datetime.now(timezone.utc).date()
    results = [_day(today.isoformat(), _metrics(api_requests=3))]
    out = aggregate(user_info=None, daily_activity=_activity(results))
    assert out["status_codes"] == {"200": 3}


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_build_budget_derives_remaining_and_pct():
    info = {
        "spend": 25.0,
        "max_budget": 100.0,
        "soft_budget": 80.0,
        "budget_duration": "30d",
        "budget_reset_at": "2026-06-01T00:00:00Z",
    }
    b = build_budget(info)
    assert b["remaining"] == pytest.approx(75.0)
    assert b["consumed_pct"] == pytest.approx(25.0)
    assert b["soft_threshold_hit"] is False


def test_build_budget_flags_soft_threshold():
    info = {"spend": 90.0, "max_budget": 100.0, "soft_budget": 80.0}
    b = build_budget(info)
    assert b["soft_threshold_hit"] is True


def test_build_budget_handles_missing_max_budget():
    b = build_budget({"spend": 10.0, "max_budget": None})
    assert b["remaining"] is None
    assert b["consumed_pct"] is None
    assert b["spend"] == pytest.approx(10.0)


def test_build_budget_handles_none_input():
    b = build_budget(None)
    assert b["spend"] == 0.0
    assert b["remaining"] is None


# ---------------------------------------------------------------------------
# Time series gap-fill
# ---------------------------------------------------------------------------


def test_time_series_fills_in_missing_days():
    base = datetime(2026, 5, 1, tzinfo=timezone.utc).date()
    results = [
        _day(base.isoformat(), _metrics(spend=1.0, total_tokens=100, api_requests=1)),
        _day(
            (base + timedelta(days=3)).isoformat(),
            _metrics(spend=2.0, total_tokens=200, api_requests=1),
        ),
    ]
    out = aggregate(user_info=None, daily_activity=_activity(results))
    series = out["time_series"]
    assert [p["date"] for p in series] == [
        "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04",
    ]
    assert series[0]["spend"] == pytest.approx(1.0)
    assert series[1]["spend"] == 0.0
    assert series[3]["spend"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Period window filter
# ---------------------------------------------------------------------------


def test_current_period_filters_by_trailing_window():
    now = datetime.now(timezone.utc).date()
    results = [
        _day((now - timedelta(days=1)).isoformat(), _metrics(spend=1.0, api_requests=1)),
        _day((now - timedelta(days=10)).isoformat(), _metrics(spend=2.0, api_requests=1)),
        _day((now - timedelta(days=60)).isoformat(), _metrics(spend=4.0, api_requests=1)),
    ]
    out = aggregate(
        user_info=None, daily_activity=_activity(results), period_days=30
    )
    assert out["lifetime"]["spend"] == pytest.approx(7.0)
    assert out["current_period"]["spend"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Tokens per request
# ---------------------------------------------------------------------------


def test_tokens_per_request_uses_total_tokens_over_requests():
    today = datetime.now(timezone.utc).date()
    results = [
        _day(today.isoformat(), _metrics(total_tokens=600, api_requests=4)),
    ]
    out = aggregate(user_info=None, daily_activity=_activity(results))
    assert out["tokens_per_request"]["avg"] == pytest.approx(150.0)
    # Percentiles aren't recoverable from a daily rollup.
    assert out["tokens_per_request"]["p50"] is None
    assert out["tokens_per_request"]["p95"] is None


def test_aggregate_handles_empty_activity():
    out = aggregate(user_info=None, daily_activity=None)
    assert out["lifetime"]["requests"] == 0
    assert out["models"] == []
    assert out["keys"] == []
    assert out["status_codes"] == {}
    assert out["time_series"] == []
